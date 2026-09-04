#!/usr/bin/env python3
"""Replay a pinned Text2SQL policy against an independent dataset split."""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evoagent.config import Settings
from evoagent.llm import JsonChatClient
from evoagent.text2sql.agentic import Text2SQLAgenticEngine
from evoagent.text2sql.benchmark import ResumableEvaluationCheckpoint
from evoagent.text2sql.evaluation import Text2SQLEvaluator, load_dataset
from evoagent.text2sql.evolution import Text2SQLEvolutionStore


def _project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _portable_artifact_path(path: Path) -> str:
    """Serialize repository paths without leaking a developer home directory."""

    resolved = path.resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return resolved.name


def _fatal_provider_outcome(outcome) -> bool:
    if str(outcome.get("failure_kind") or "") != "FRAMEWORK_ERROR":
        return False
    error = str(outcome.get("framework_error") or "").lower()
    return any(
        marker in error
        for marker in (
            "arrearage",
            "overdue-payment",
            "insufficient_quota",
            "invalidapikey",
            "invalid api key",
            "http 401",
            "http 403",
            "http 429",
            "rate limit",
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset",
        type=Path,
        default=PROJECT_ROOT / "evaluation" / "datasets" / "text2sql_v1",
    )
    parser.add_argument(
        "--split",
        action="append",
        choices=("train", "validation", "sealed_holdout"),
        default=None,
    )
    parser.add_argument(
        "--case-id",
        action="append",
        default=[],
        help="Evaluate an exact case id; repeat to build a deterministic smoke subset.",
    )
    parser.add_argument("--max-cases", type=int, default=None)
    parser.add_argument("--principal", action="append", default=["local-user"])
    parser.add_argument(
        "--database",
        type=Path,
        default=PROJECT_ROOT / "database" / "evo_text2sql_eval.sqlite3",
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "text2sql" / "schema" / "database_snapshot.json",
    )
    parser.add_argument(
        "--knowledge-store",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "text2sql" / "knowledge" / "knowledge.sqlite3",
    )
    parser.add_argument(
        "--vanna-index-root",
        type=Path,
        default=_project_path(
            os.getenv(
                "EVOAGENT_TEXT2SQL_VANNA_ROOT",
                "artifacts/text2sql/vanna",
            )
        ),
        help="Pinned retrieval-only Vanna index root used by the live Text2SQL runtime.",
    )
    parser.add_argument(
        "--evolution-store",
        type=Path,
        default=_project_path(
            os.getenv(
                "EVOAGENT_TEXT2SQL_EVOLUTION_STORE",
                "artifacts/text2sql/evolution/evolution.sqlite3",
            )
        ),
    )
    parser.add_argument(
        "--policy-version",
        default="",
        help="Evaluate a specific stored candidate; defaults to the active policy.",
    )
    parser.add_argument(
        "--memory-candidate-id",
        default="",
        help=(
            "Evaluate one human-approved Semantic Memory candidate without "
            "making it visible to the stable runtime."
        ),
    )
    parser.add_argument(
        "--experience-candidate-id",
        default="",
        help="Bind this artifact to one isolated Question-SQL candidate.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "text2sql" / "evaluation" / "latest.json",
    )
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=None,
        help="Append-only resume log; defaults to <output>.checkpoint.jsonl.",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--resume-from",
        type=Path,
        default=None,
        help=(
            "Seed a new checkpoint from an incomplete checkpoint with the same "
            "identity, retaining only non-framework-error outcomes."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help=(
            "Number of independent evaluation cases to run concurrently. "
            "Checkpoint writes remain serialized; budget limits may overshoot "
            "by at most this many in-flight cases."
        ),
    )
    parser.add_argument(
        "--max-total-tokens",
        type=int,
        default=0,
        help="Stop before the next case after this measured token budget is reached; 0 disables.",
    )
    parser.add_argument(
        "--max-llm-calls",
        type=int,
        default=0,
        help="Stop before the next case after this model-call budget is reached; 0 disables.",
    )
    parser.add_argument(
        "--max-estimated-cost-usd",
        type=float,
        default=0.0,
        help="Case-boundary USD budget using the configured token rates; 0 disables.",
    )
    parser.add_argument("--input-cost-per-million", type=float, default=0.0)
    parser.add_argument("--output-cost-per-million", type=float, default=0.0)
    args = parser.parse_args()

    settings = Settings.from_env()
    llm = settings.resolved_llm()
    if not llm:
        parser.error("a configured LLM is required for agentic evaluation")
    splits = args.split or ["validation"]
    bundle = load_dataset(args.dataset, splits)
    cases = list(bundle.cases)
    if args.case_id:
        requested_ids = list(dict.fromkeys(str(value) for value in args.case_id))
        by_id = {case.case_id: case for case in cases}
        unknown_ids = [case_id for case_id in requested_ids if case_id not in by_id]
        if unknown_ids:
            parser.error(
                "case id is not present in the selected split(s): %s"
                % ", ".join(unknown_ids)
            )
        cases = [by_id[case_id] for case_id in requested_ids]
    maximum = settings.eval_max_cases if args.max_cases is None else args.max_cases
    if maximum < 0:
        parser.error("--max-cases cannot be negative")
    if args.workers <= 0:
        parser.error("--workers must be positive")
    if args.resume and args.resume_from is not None:
        parser.error("--resume and --resume-from are mutually exclusive")
    if args.max_total_tokens < 0 or args.max_llm_calls < 0:
        parser.error("token and model-call budgets cannot be negative")
    if (
        args.max_estimated_cost_usd < 0
        or args.input_cost_per_million < 0
        or args.output_cost_per_million < 0
    ):
        parser.error("cost budgets and rates cannot be negative")
    if args.max_estimated_cost_usd and not (
        args.input_cost_per_million or args.output_cost_per_million
    ):
        parser.error("--max-estimated-cost-usd requires at least one token price")
    if maximum:
        cases = cases[:maximum]

    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    client = JsonChatClient(
        str(llm["base_url"]),
        str(llm["api_key"]),
        str(llm["model"]),
        provider=str(llm["provider"]),
        timeout=settings.agent_time_budget_seconds,
        extra_headers=dict(llm.get("headers") or {}),
    )
    with Text2SQLEvolutionStore(args.evolution_store, snapshot) as evolution:
        policy = evolution.get_policy(args.policy_version or None)
        memory_candidate_id = args.memory_candidate_id.strip()
        if memory_candidate_id:
            memory_snapshot_id = evolution.memory_snapshot_id_for(
                memory_candidate_id
            )

            def memory_provider(skill, limit):
                return evolution.evaluation_memory(
                    skill, memory_candidate_id, limit
                )
        else:
            memory_snapshot_id = evolution.memory_snapshot_id
            memory_provider = evolution.stable_memory
        engine = Text2SQLAgenticEngine(
            client=client,
            database_path=args.database,
            snapshot=snapshot,
            knowledge_store_path=args.knowledge_store,
            vanna_index_root=args.vanna_index_root,
            principals=args.principal,
            memory_snapshot_id=memory_snapshot_id,
            policy_version=policy.version,
            policy_artifact=policy,
            stable_memory_provider=memory_provider,
            token_budget=settings.agent_token_budget,
            time_budget=settings.agent_time_budget_seconds,
        )
        identity = {
            "dataset_id": bundle.dataset_id,
            "dataset_sha256": bundle.dataset_sha256,
            "case_ids": [case.case_id for case in cases],
            "evaluated_splits": splits,
            "version_pins": engine.version_pins,
            "model": {
                "provider": llm["provider"],
                "model": llm["model"],
                "temperature": 0,
            },
        }
        checkpoint_path = args.checkpoint or args.output.with_suffix(
            args.output.suffix + ".checkpoint.jsonl"
        )
        checkpoint = ResumableEvaluationCheckpoint(checkpoint_path, identity)
        if args.resume_from is None:
            existing_outcomes = checkpoint.start(args.resume)
        else:
            existing_outcomes = checkpoint.start(False)
            source = ResumableEvaluationCheckpoint(args.resume_from, identity)
            source_outcomes = source.start(True)
            existing_outcomes = tuple(
                dict(item)
                for item in source_outcomes
                if str(item.get("failure_kind") or "") != "FRAMEWORK_ERROR"
            )
            for outcome in existing_outcomes:
                checkpoint.append_outcome(outcome)

        def persist_outcome(outcome):
            if _fatal_provider_outcome(outcome):
                raise RuntimeError(
                    "fatal model-provider condition; case was not checkpointed: %s"
                    % str(outcome.get("framework_error") or "")[:500]
                )
            checkpoint.append_outcome(outcome)

        def usage(outcomes):
            input_tokens = sum(int(item.get("input_tokens") or 0) for item in outcomes)
            output_tokens = sum(int(item.get("output_tokens") or 0) for item in outcomes)
            reported_cost = sum(
                float(item.get("reported_cost_usd") or 0.0) for item in outcomes
            )
            estimated_cost = (
                input_tokens * args.input_cost_per_million / 1_000_000
                + output_tokens * args.output_cost_per_million / 1_000_000
            )
            return {
                "cases": len(outcomes),
                "llm_calls": sum(int(item.get("llm_calls") or 0) for item in outcomes),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "reported_cost_usd": round(reported_cost, 8),
                "estimated_cost_usd": round(estimated_cost, 8),
                "budget_cost_usd": round(max(reported_cost, estimated_cost), 8),
            }

        def should_continue(outcomes):
            current = usage(outcomes)
            if args.max_total_tokens and current["total_tokens"] >= args.max_total_tokens:
                return False
            if args.max_llm_calls and current["llm_calls"] >= args.max_llm_calls:
                return False
            if (
                args.max_estimated_cost_usd
                and current["budget_cost_usd"] >= args.max_estimated_cost_usd
            ):
                return False
            return True

        evaluator = Text2SQLEvaluator(args.database, snapshot, engine.version_pins)
        if args.workers == 1:
            report = evaluator.evaluate(
                cases,
                engine.run,
                redact_holdout=True,
                existing_outcomes=existing_outcomes,
                progress_callback=persist_outcome,
                should_continue=should_continue,
            )
        else:
            outcomes = [dict(item) for item in existing_outcomes]
            completed_ids = {str(item.get("case_id") or "") for item in outcomes}
            remaining = iter(
                case for case in cases if case.case_id not in completed_ids
            )

            def evaluate_one(case):
                one = evaluator.evaluate(
                    (case,),
                    engine.run,
                    redact_holdout=True,
                )
                return dict(one["outcomes"][0])

            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                in_flight = {}

                def fill_slots():
                    while len(in_flight) < args.workers and should_continue(outcomes):
                        try:
                            case = next(remaining)
                        except StopIteration:
                            return
                        in_flight[pool.submit(evaluate_one, case)] = case.case_id

                fill_slots()
                while in_flight:
                    done, _ = wait(tuple(in_flight), return_when=FIRST_COMPLETED)
                    for future in done:
                        in_flight.pop(future)
                        outcome = future.result()
                        persist_outcome(outcome)
                        outcomes.append(outcome)
                    fill_slots()

            # All completed cases are supplied back to the evaluator so the
            # canonical metric aggregation remains identical to serial runs.
            report = evaluator.evaluate(
                cases,
                engine.run,
                redact_holdout=True,
                existing_outcomes=outcomes,
                should_continue=lambda _outcomes: False,
            )
    complete = len(report["outcomes"]) == len(cases)
    measured_usage = usage(report["outcomes"])
    artifact = {
        "contract_version": 1,
        "status": "complete" if complete else "incomplete_budget_reached",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset_id": bundle.dataset_id,
        "dataset_sha256": bundle.dataset_sha256,
        "evaluated_splits": splits,
        "evaluated_case_count": len(cases),
        "model": {"provider": llm["provider"], "model": llm["model"], "temperature": 0},
        "memory_candidate_id": args.memory_candidate_id.strip(),
        "experience_candidate_id": args.experience_candidate_id.strip(),
        "checkpoint": _portable_artifact_path(checkpoint_path),
        "resume_from": _portable_artifact_path(args.resume_from) if args.resume_from else "",
        "budget": {
            "enforcement": (
                "between_cases"
                if args.workers == 1
                else "between_completed_cases_with_in_flight_overshoot"
            ),
            "workers": args.workers,
            "max_cases": maximum,
            "max_total_tokens": args.max_total_tokens,
            "max_llm_calls": args.max_llm_calls,
            "max_estimated_cost_usd": args.max_estimated_cost_usd,
            "input_cost_per_million": args.input_cost_per_million,
            "output_cost_per_million": args.output_cost_per_million,
            "measured": measured_usage,
        },
        "report": report,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    if complete:
        checkpoint.mark_complete(artifact)
    print(json.dumps({key: artifact[key] for key in artifact if key != "report"}, ensure_ascii=False, indent=2))
    print(json.dumps(report["overall"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
