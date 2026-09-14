#!/usr/bin/env python3
"""Inspect and operate the human-gated Text2SQL evolution control plane."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evoagent.text2sql.evolution import Text2SQLEvolutionStore
from evoagent.text2sql.auto_evolution import prepare_automatic_policy_candidate
from evoagent.text2sql.evaluation import load_dataset
from evoagent.text2sql.agentic import build_runtime_identity
from evoagent.text2sql.policy_generator import Text2SQLPolicyCandidateGenerator
from evoagent.text2sql.semantic_rules import generate_semantic_rule, propose_policy_from_rules
from evoagent.text2sql.policy import TEXT2SQL_SKILLS
from evoagent.text2sql.shadow import Text2SQLShadowReleaseManager
from evoagent.text2sql.vanna_retriever import VannaRetrieverOnly
from evoagent.config import Settings
from evoagent.llm import JsonChatClient


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _vanna_pin(root: Path, snapshot: dict) -> tuple[str, bool]:
    version = VannaRetrieverOnly.current_index_version(root)
    if not version:
        raise RuntimeError("Vanna corpus is not built")
    status = VannaRetrieverOnly(root, version).status()
    if (
        not status.get("ready")
        or status.get("database_snapshot_id") != snapshot["snapshot_id"]
    ):
        raise RuntimeError("Vanna corpus is not ready for the pinned Schema Snapshot")
    return version, True


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    root.add_argument(
        "--snapshot",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "text2sql" / "schema" / "database_snapshot.json",
    )
    root.add_argument(
        "--store",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "text2sql" / "evolution" / "evolution.sqlite3",
    )
    root.add_argument(
        "--vanna-root",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "text2sql" / "vanna",
    )
    root.add_argument("--principal", action="append", default=["local-user"])
    root.add_argument(
        "--review-key-file",
        type=Path,
        default=None,
        help="Human-held dataset review signing key; can also use EVOAGENT_TEXT2SQL_REVIEW_KEY_FILE.",
    )
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("list-policies")

    rule_generate = commands.add_parser(
        "rule-generate", help="Extract one role-scoped SemanticRule offline"
    )
    rule_generate.add_argument("--memory-id", required=True)
    rule_generate.add_argument("--actor", required=True)
    rule_list = commands.add_parser("rule-list")
    rule_list.add_argument("--state", choices=("candidate", "confirmed", "rejected"), default="")
    rule_review = commands.add_parser("rule-review")
    rule_review.add_argument("--rule-id", required=True)
    rule_review.add_argument("--decision", choices=("confirm", "reject"), required=True)
    rule_review.add_argument("--actor", required=True)
    rule_review.add_argument("--review-note", default="")
    rule_review.add_argument("--human-reviewed", action="store_true", required=True)
    rule_policy = commands.add_parser("policy-from-rules")
    rule_policy.add_argument("--rule-id", action="append", required=True)
    rule_policy.add_argument("--actor", required=True)
    rule_policy.add_argument("--reason", default="")
    rule_policy.add_argument("--parent", default="")

    export = commands.add_parser("export-policy")
    export.add_argument("--version", default="")
    export.add_argument("--output", type=Path, required=True)

    propose = commands.add_parser("propose")
    propose.add_argument("--artifact", type=Path, required=True)
    propose.add_argument("--skill", choices=TEXT2SQL_SKILLS, required=True)
    propose.add_argument("--reason", required=True)
    propose.add_argument("--actor", required=True)
    propose.add_argument("--parent", default="")

    auto_propose = commands.add_parser("auto-propose")
    auto_propose.add_argument(
        "--memory-id",
        action="append",
        required=True,
        help="Confirmed Experience id; repeat to select multiple sources.",
    )
    auto_propose.add_argument(
        "--skill",
        choices=TEXT2SQL_SKILLS,
        default="",
        help="Optional owner assertion; normally inferred from the Experiences.",
    )
    auto_propose.add_argument("--reason", default="")
    auto_propose.add_argument("--actor", required=True)
    auto_propose.add_argument("--parent", default="")

    auto_prepare = commands.add_parser(
        "auto-prepare",
        help=(
            "Admit one machine-verifiable Experience, derive a SemanticRule, "
            "and compile a role-scoped Prompt candidate."
        ),
    )
    auto_prepare.add_argument("--memory-id", required=True)
    auto_prepare.add_argument("--actor", default="text2sql-auto-evolution")
    auto_pending = commands.add_parser(
        "auto-prepare-pending",
        help="Prepare every eligible pending Experience, up to a bounded limit.",
    )
    auto_pending.add_argument("--limit", type=int, default=10)
    auto_pending.add_argument("--actor", default="text2sql-auto-evolution")

    evaluate = commands.add_parser("record-evaluation")
    evaluate.add_argument("--candidate", required=True)
    evaluate.add_argument("--dataset-manifest", type=Path, required=True)
    evaluate.add_argument("--baseline-report", type=Path, required=True)
    evaluate.add_argument("--candidate-report", type=Path, required=True)
    evaluate.add_argument(
        "--auto-activate",
        action="store_true",
        help="Atomically activate when all release gates pass and runtime identity is unchanged.",
    )
    evaluate.add_argument("--actor", default="text2sql-auto-evolution")

    approve = commands.add_parser("approve")
    approve.add_argument("--candidate", required=True)
    approve.add_argument("--actor", required=True)
    approve.add_argument("--reason", required=True)
    approve.add_argument("--human-approved", action="store_true", required=True)

    auto_activate = commands.add_parser(
        "auto-activate",
        help=(
            "Activate only after Target Replay and the complete independent "
            "Validation/Holdout gate have passed."
        ),
    )
    auto_activate.add_argument("--candidate", required=True)
    auto_activate.add_argument("--actor", default="text2sql-auto-evolution")
    auto_activate.add_argument(
        "--reason",
        default="All automatic release gates passed without regression.",
    )

    rollback = commands.add_parser("rollback")
    rollback.add_argument("--target", required=True)
    rollback.add_argument("--actor", required=True)
    rollback.add_argument("--reason", required=True)

    memory_add = commands.add_parser("memory-add")
    memory_add.add_argument("--skill", choices=TEXT2SQL_SKILLS, required=True)
    memory_add.add_argument("--failure-kind", required=True)
    memory_add.add_argument("--content", required=True)
    memory_add.add_argument("--origin", choices=("train", "production_feedback"), default="production_feedback")
    memory_add.add_argument("--evidence", type=Path)

    memory_list = commands.add_parser("memory-list")
    memory_list.add_argument(
        "--state",
        choices=(
            "candidate",
            "confirmed",
            "needs_evidence",
            "approved",
            "evaluating",
            "evaluated",
            "evaluation_failed",
            "stable",
            "rejected",
            "retired",
        ),
        default="",
    )

    memory_review = commands.add_parser("memory-review")
    memory_review.add_argument("--memory-id", required=True)
    memory_review.add_argument(
        "--decision",
        choices=("approve", "confirm", "reject", "needs_evidence"),
        required=True,
    )
    memory_review.add_argument("--actor", required=True)
    memory_review.add_argument("--human-reviewed", action="store_true", required=True)
    memory_review.add_argument("--review-note", default="")

    memory_activate = commands.add_parser("memory-activate")
    memory_activate.add_argument("--memory-id", required=True)
    memory_activate.add_argument("--actor", required=True)
    memory_activate.add_argument("--reason", required=True)
    memory_activate.add_argument("--human-approved", action="store_true", required=True)

    memory_rollback = commands.add_parser("memory-rollback")
    memory_rollback.add_argument("--memory-id", required=True)
    memory_rollback.add_argument("--actor", required=True)
    memory_rollback.add_argument("--reason", required=True)

    capture = commands.add_parser("capture-training-failures")
    capture.add_argument("--report", type=Path, required=True)
    capture.add_argument("--skill", choices=TEXT2SQL_SKILLS, required=True)

    shadow_configure = commands.add_parser("shadow-configure")
    shadow_configure.add_argument("--candidate", required=True)
    shadow_configure.add_argument("--actor", required=True)
    shadow_configure.add_argument("--percent", type=int, default=5)
    shadow_configure.add_argument("--min-samples", type=int, default=20)
    shadow_configure.add_argument("--max-failure-rate", type=float, default=0.0)
    shadow_configure.add_argument("--max-result-disagreement", type=float, default=0.2)
    shadow_configure.add_argument("--max-p95-multiplier", type=float, default=1.2)

    shadow_status = commands.add_parser("shadow-status")
    shadow_status.add_argument("--deployment", default="")

    shadow_observations = commands.add_parser("shadow-observations")
    shadow_observations.add_argument("--deployment", required=True)
    shadow_observations.add_argument(
        "--review-state", choices=("pending", "reviewed", "not_required"), default=""
    )
    shadow_observations.add_argument("--limit", type=int, default=100)

    shadow_review = commands.add_parser("shadow-review")
    shadow_review.add_argument("--observation", required=True)
    shadow_review.add_argument(
        "--verdict",
        choices=("equivalent", "candidate_better", "stable_better", "reject"),
        required=True,
    )
    shadow_review.add_argument("--actor", required=True)
    shadow_review.add_argument("--reason", required=True)
    shadow_review.add_argument("--human-reviewed", action="store_true", required=True)

    shadow_approve = commands.add_parser("shadow-approve")
    shadow_approve.add_argument("--deployment", required=True)
    shadow_approve.add_argument("--actor", required=True)
    shadow_approve.add_argument("--reason", required=True)
    shadow_approve.add_argument("--human-approved", action="store_true", required=True)

    canary_start = commands.add_parser("canary-start")
    canary_start.add_argument("--deployment", required=True)
    canary_start.add_argument("--actor", required=True)
    canary_start.add_argument("--percent", type=int, default=5)
    canary_start.add_argument("--min-samples", type=int, default=20)
    return root


def main() -> int:
    args = parser().parse_args()
    snapshot = _json(args.snapshot)
    with Text2SQLEvolutionStore(args.store, snapshot) as store:
        shadow = Text2SQLShadowReleaseManager(store)
        if args.command == "status":
            output = {
                "active_policy_version": store.active_policy_version,
                "memory_snapshot_id": store.memory_snapshot_id,
                "stable_memory_count": len(store.list_memory("stable")),
                "candidate_memory_count": len(store.list_memory("candidate")),
                "release": shadow.status(),
            }
        elif args.command == "list-policies":
            output = store.list_policies()
        elif args.command == "rule-list":
            output = store.list_semantic_rules(args.state)
        elif args.command == "rule-review":
            output = store.review_semantic_rule(args.rule_id, args.decision, args.actor, args.review_note)
        elif args.command in {
            "rule-generate",
            "policy-from-rules",
            "auto-prepare",
            "auto-prepare-pending",
        }:
            settings = Settings.from_env()
            llm = settings.resolved_llm()
            if not llm:
                raise RuntimeError("a configured LLM is required for rule generation and compilation")
            client = JsonChatClient(str(llm["base_url"]), str(llm["api_key"]), str(llm["model"]),
                                   provider=str(llm["provider"]), timeout=settings.agent_time_budget_seconds,
                                   extra_headers=dict(llm.get("headers") or {}))
            if args.command == "rule-generate":
                output = generate_semantic_rule(store, client, args.memory_id, args.actor,
                                               settings.agent_token_budget)
            elif args.command == "auto-prepare":
                output = prepare_automatic_policy_candidate(
                    store,
                    client,
                    args.memory_id,
                    actor=args.actor,
                    token_budget=settings.agent_token_budget,
                )
            elif args.command == "auto-prepare-pending":
                if args.limit <= 0 or args.limit > 50:
                    raise ValueError("auto-prepare-pending limit must be between 1 and 50")
                prepared = []
                blocked = []
                pending = list(store.list_memory("candidate"))
                pending.extend(store.confirmed_experiences(limit=args.limit))
                unique_pending = []
                seen_memory_ids = set()
                for item in pending:
                    memory_id = str(item.get("memory_id") or "")
                    if not memory_id or memory_id in seen_memory_ids:
                        continue
                    seen_memory_ids.add(memory_id)
                    unique_pending.append(item)
                    if len(unique_pending) >= args.limit:
                        break
                for item in unique_pending:
                    try:
                        prepared.append(
                            prepare_automatic_policy_candidate(
                                store,
                                client,
                                str(item["memory_id"]),
                                actor=args.actor,
                                token_budget=settings.agent_token_budget,
                            )
                        )
                    except ValueError as exc:
                        blocked.append(
                            {
                                "memory_id": str(item.get("memory_id") or ""),
                                "reason": str(exc)[:1000],
                            }
                        )
                output = {
                    "prepared": prepared,
                    "blocked": blocked,
                    "prepared_count": len(prepared),
                    "blocked_count": len(blocked),
                }
            else:
                output = propose_policy_from_rules(store, client, args.rule_id, args.actor,
                    args.reason, settings.agent_token_budget, args.parent)
        elif args.command == "export-policy":
            artifact = store.get_policy(args.version or None).as_dict()
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(artifact, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            output = {
                "policy_version": store.get_policy(args.version or None).version,
                "output": str(args.output.resolve()),
            }
        elif args.command == "propose":
            output = {
                "candidate_policy_version": store.propose_policy(
                    _json(args.artifact), args.skill, args.reason, args.actor, args.parent
                )
            }
        elif args.command == "auto-propose":
            settings = Settings.from_env()
            llm = settings.resolved_llm()
            if not llm:
                raise RuntimeError("a configured LLM is required for auto-propose")
            client = JsonChatClient(
                str(llm["base_url"]),
                str(llm["api_key"]),
                str(llm["model"]),
                provider=str(llm["provider"]),
                timeout=settings.agent_time_budget_seconds,
                extra_headers=dict(llm.get("headers") or {}),
            )
            parent = store.get_policy(args.parent or None)
            experiences = tuple(
                store.get_memory(memory_id) for memory_id in args.memory_id
            )
            generated = Text2SQLPolicyCandidateGenerator(
                client, settings.agent_token_budget
            ).generate_from_confirmed_experiences(
                experiences,
                parent,
                snapshot,
                target_agent=args.skill,
            )
            candidate_version = store.propose_policy(
                generated["artifact"],
                generated["target_agent"],
                args.reason or generated["rationale"] or "Root-cause policy proposal",
                args.actor,
                parent.version,
                {**{
                    key: value
                    for key, value in generated.items()
                    if key not in {"artifact", "policy_version"}
                },
                    "source": "confirmed-experiences",
                    "contract": "ExperiencePolicyProposal/v1",
                    "target_replay_required": True,
                },
            )
            output = {
                "candidate_policy_version": candidate_version,
                "target_skill": generated["target_agent"],
                "memory_ids": list(generated["memory_ids"]),
                "rationale": generated["rationale"],
                "clusters": generated["clusters"],
                "generation": generated["generation"],
                "target_replay_status": "pending",
                "next_step": "run_target_replay",
            }
        elif args.command == "record-evaluation":
            baseline = _json(args.baseline_report)
            candidate = _json(args.candidate_report)
            manifest = _json(args.dataset_manifest)
            review_key = None
            if args.review_key_file is not None:
                from evoagent.text2sql.dataset_review import read_review_signing_key

                review_key = read_review_signing_key(args.review_key_file)
            verified_dataset = load_dataset(
                args.dataset_manifest.parent, review_signing_key=review_key
            )
            if (
                verified_dataset.dataset_id != manifest.get("dataset_id")
                or verified_dataset.dataset_sha256 != manifest.get("dataset_sha256")
            ):
                raise ValueError("dataset manifest failed integrity verification")
            output = store.record_evaluation(
                args.candidate,
                manifest,
                baseline,
                candidate,
                verified_dataset.review_evidence,
            )
            if args.auto_activate and output.get("candidate_status") == "shadow_ready":
                wiki_version, vanna_ready = _vanna_pin(args.vanna_root, snapshot)
                settings = Settings.from_env()
                llm = settings.resolved_llm()
                if not llm:
                    raise RuntimeError("configured LLM is required for Policy activation")
                principals = sorted(set(str(value) for value in args.principal))
                current_version_pins = {
                    "database_snapshot_id": snapshot["snapshot_id"],
                    "wiki_index_version": wiki_version,
                    "vanna_index_version": (
                        wiki_version if vanna_ready else "fallback:%s" % wiki_version
                    ),
                    "memory_snapshot_id": store.memory_snapshot_id,
                    "policy_version": store.active_policy_version,
                }
                current_evaluation_identity = {
                    "model": {
                        "provider": str(llm["provider"]),
                        "model": str(llm["model"]),
                        "temperature": 0,
                    },
                    "runtime": dict(
                        build_runtime_identity(
                            token_budget=settings.agent_token_budget,
                            time_budget=settings.agent_time_budget_seconds,
                            policy_source_memory_ids=store.policy_source_memory_ids(
                                store.active_policy_version
                            ),
                        )
                    ),
                    "principals": principals,
                }
                store.activate_policy_automatically(
                    args.candidate,
                    args.actor,
                    "Target Replay and independent release evaluation passed.",
                    current_version_pins=current_version_pins,
                    current_evaluation_identity=current_evaluation_identity,
                )
                output = {
                    **dict(output),
                    "automatic_activation": "activated",
                    "active_policy_version": store.active_policy_version,
                }
        elif args.command in {"approve", "auto-activate"}:
            wiki_version, vanna_ready = _vanna_pin(args.vanna_root, snapshot)
            settings = Settings.from_env()
            llm = settings.resolved_llm()
            if not llm:
                raise RuntimeError("configured LLM is required for Policy activation")
            principals = sorted(set(str(value) for value in args.principal))
            current_version_pins = {
                "database_snapshot_id": snapshot["snapshot_id"],
                "wiki_index_version": wiki_version,
                "vanna_index_version": (
                    wiki_version
                    if vanna_ready
                    else "fallback:%s" % wiki_version
                ),
                "memory_snapshot_id": store.memory_snapshot_id,
                "policy_version": store.active_policy_version,
            }
            current_evaluation_identity = {
                "model": {
                    "provider": str(llm["provider"]),
                    "model": str(llm["model"]),
                    "temperature": 0,
                },
                "runtime": dict(
                    build_runtime_identity(
                        token_budget=settings.agent_token_budget,
                        time_budget=settings.agent_time_budget_seconds,
                        policy_source_memory_ids=store.policy_source_memory_ids(
                            store.active_policy_version
                        ),
                    )
                ),
                "principals": principals,
            }
            if args.command == "approve":
                store.activate_policy(
                    args.candidate,
                    args.actor,
                    args.reason,
                    args.human_approved,
                    current_version_pins=current_version_pins,
                    current_evaluation_identity=current_evaluation_identity,
                )
            else:
                store.activate_policy_automatically(
                    args.candidate,
                    args.actor,
                    args.reason,
                    current_version_pins=current_version_pins,
                    current_evaluation_identity=current_evaluation_identity,
                )
            output = {"active_policy_version": store.active_policy_version}
        elif args.command == "rollback":
            store.rollback(args.target, args.actor, args.reason)
            output = {"active_policy_version": store.active_policy_version}
        elif args.command == "memory-add":
            output = {
                "memory_id": store.add_memory_candidate(
                    args.skill,
                    args.failure_kind,
                    args.content,
                    _json(args.evidence) if args.evidence else {},
                    args.origin,
                ),
                "state": "candidate",
            }
        elif args.command == "memory-list":
            output = store.list_memory(args.state)
        elif args.command == "memory-review":
            current = store.get_memory(args.memory_id)
            if (current.get("rule") or {}).get("contract") == "ExperienceMemory/v1":
                if not args.human_reviewed:
                    raise ValueError("explicit human review is required")
                decision = "confirm" if args.decision == "approve" else args.decision
                reviewed = store.review_experience_memory(
                    args.memory_id,
                    decision,
                    args.actor,
                    args.review_note,
                )
            else:
                if args.decision not in {"approve", "reject"}:
                    raise ValueError("legacy Memory accepts approve or reject only")
                reviewed = store.review_memory(
                    args.memory_id,
                    args.decision,
                    args.actor,
                    args.human_reviewed,
                    args.review_note,
                )
            output = {
                "memory_id": args.memory_id,
                "decision": args.decision,
                "state": reviewed["state"],
                "memory_snapshot_id": store.memory_snapshot_id,
            }
        elif args.command == "memory-activate":
            wiki_version, vanna_ready = _vanna_pin(args.vanna_root, snapshot)
            settings = Settings.from_env()
            llm = settings.resolved_llm()
            if not llm:
                raise RuntimeError("configured LLM is required for Memory activation")
            principals = sorted(set(str(value) for value in args.principal))
            output = store.activate_memory(
                args.memory_id,
                args.actor,
                args.reason,
                args.human_approved,
                current_version_pins={
                    "database_snapshot_id": snapshot["snapshot_id"],
                    "wiki_index_version": wiki_version,
                    "vanna_index_version": (
                        wiki_version
                        if vanna_ready
                        else "fallback:%s" % wiki_version
                    ),
                    "memory_snapshot_id": store.memory_snapshot_id,
                    "policy_version": store.active_policy_version,
                },
                current_evaluation_identity={
                    "model": {
                        "provider": str(llm["provider"]),
                        "model": str(llm["model"]),
                        "temperature": 0,
                    },
                    "runtime": dict(
                        build_runtime_identity(
                            token_budget=settings.agent_token_budget,
                            time_budget=settings.agent_time_budget_seconds,
                            policy_source_memory_ids=store.policy_source_memory_ids(
                                store.active_policy_version
                            ),
                        )
                    ),
                    "principals": principals,
                },
            )
        elif args.command == "memory-rollback":
            output = store.rollback_memory(
                args.memory_id, args.actor, args.reason
            )
        elif args.command == "capture-training-failures":
            report = _json(args.report)
            memory_ids = store.capture_training_failures(
                report.get("report", report), args.skill
            )
            output = {"created_or_existing": len(memory_ids), "memory_ids": memory_ids}
        elif args.command == "shadow-configure":
            wiki_version, vanna_ready = _vanna_pin(args.vanna_root, snapshot)
            settings = Settings.from_env()
            llm = settings.resolved_llm()
            if not llm:
                raise RuntimeError("configured LLM is required for shadow release")
            principals = sorted(set(str(value) for value in args.principal))
            output = shadow.configure_shadow(
                args.candidate,
                {
                    "database_snapshot_id": snapshot["snapshot_id"],
                    "wiki_index_version": wiki_version,
                    "vanna_index_version": (
                        wiki_version
                        if vanna_ready
                        else "fallback:%s" % wiki_version
                    ),
                    "memory_snapshot_id": store.memory_snapshot_id,
                    "policy_version": store.active_policy_version,
                },
                args.actor,
                args.percent,
                args.min_samples,
                args.max_failure_rate,
                args.max_result_disagreement,
                args.max_p95_multiplier,
                current_evaluation_identity={
                    "model": {
                        "provider": str(llm["provider"]),
                        "model": str(llm["model"]),
                        "temperature": 0,
                    },
                    "runtime": dict(
                        build_runtime_identity(
                            token_budget=settings.agent_token_budget,
                            time_budget=settings.agent_time_budget_seconds,
                            policy_source_memory_ids=store.policy_source_memory_ids(
                                store.active_policy_version
                            ),
                        )
                    ),
                    "principals": principals,
                },
            )
        elif args.command == "shadow-status":
            output = shadow.status(args.deployment)
        elif args.command == "shadow-observations":
            output = shadow.list_observations(
                args.deployment, args.review_state, args.limit
            )
        elif args.command == "shadow-review":
            shadow.review_observation(
                args.observation,
                args.verdict,
                args.actor,
                args.reason,
                args.human_reviewed,
            )
            output = {"observation_id": args.observation, "verdict": args.verdict}
        elif args.command == "shadow-approve":
            shadow.approve_shadow(
                args.deployment, args.actor, args.reason, args.human_approved
            )
            output = shadow.status(args.deployment)
        elif args.command == "canary-start":
            shadow.start_canary(
                args.deployment, args.actor, args.percent, args.min_samples
            )
            output = shadow.status(args.deployment)
        else:
            raise AssertionError("unreachable command")
    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
