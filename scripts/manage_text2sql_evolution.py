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
from evoagent.text2sql.evaluation import load_dataset
from evoagent.text2sql.knowledge_store import KnowledgeStore
from evoagent.text2sql.policy_generator import Text2SQLPolicyCandidateGenerator
from evoagent.text2sql.policy import TEXT2SQL_SKILLS
from evoagent.text2sql.shadow import Text2SQLShadowReleaseManager
from evoagent.config import Settings
from evoagent.llm import JsonChatClient


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


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
        "--knowledge-store",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "text2sql" / "knowledge" / "knowledge.sqlite3",
    )
    root.add_argument(
        "--review-key-file",
        type=Path,
        default=None,
        help="Human-held dataset review signing key; can also use EVOAGENT_TEXT2SQL_REVIEW_KEY_FILE.",
    )
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("list-policies")

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
    auto_propose.add_argument("--skill", choices=TEXT2SQL_SKILLS, required=True)
    auto_propose.add_argument("--reason", default="")
    auto_propose.add_argument("--actor", required=True)
    auto_propose.add_argument("--parent", default="")

    evaluate = commands.add_parser("record-evaluation")
    evaluate.add_argument("--candidate", required=True)
    evaluate.add_argument("--dataset-manifest", type=Path, required=True)
    evaluate.add_argument("--baseline-report", type=Path, required=True)
    evaluate.add_argument("--candidate-report", type=Path, required=True)

    approve = commands.add_parser("approve")
    approve.add_argument("--candidate", required=True)
    approve.add_argument("--actor", required=True)
    approve.add_argument("--reason", required=True)
    approve.add_argument("--human-approved", action="store_true", required=True)

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
    memory_review.add_argument("--decision", choices=("approve", "reject"), required=True)
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
            generated = Text2SQLPolicyCandidateGenerator(
                client, settings.agent_token_budget
            ).generate(
                store.stable_memory(args.skill, 20), parent, args.skill, snapshot
            )
            candidate_version = store.propose_policy(
                generated["artifact"],
                args.skill,
                args.reason or generated["rationale"] or "Root-cause policy proposal",
                args.actor,
                args.parent,
                {
                    key: value
                    for key, value in generated.items()
                    if key not in {"artifact", "policy_version"}
                },
            )
            output = {
                "candidate_policy_version": candidate_version,
                "target_skill": args.skill,
                "rationale": generated["rationale"],
                "clusters": generated["clusters"],
                "generation": generated["generation"],
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
        elif args.command == "approve":
            store.activate_policy(
                args.candidate, args.actor, args.reason, args.human_approved
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
            output = store.activate_memory(
                args.memory_id,
                args.actor,
                args.reason,
                args.human_approved,
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
            with KnowledgeStore(args.knowledge_store) as knowledge:
                wiki_version = knowledge.current_index_version("stable")
            output = shadow.configure_shadow(
                args.candidate,
                {
                    "database_snapshot_id": snapshot["snapshot_id"],
                    "wiki_index_version": wiki_version,
                    "memory_snapshot_id": store.memory_snapshot_id,
                    "policy_version": store.active_policy_version,
                },
                args.actor,
                args.percent,
                args.min_samples,
                args.max_failure_rate,
                args.max_result_disagreement,
                args.max_p95_multiplier,
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
