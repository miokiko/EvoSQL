#!/usr/bin/env python3
"""Run one question through EvoSQL's five-agent, 11-node Text2SQL protocol."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evoagent.config import Settings
from evoagent.llm import JsonChatClient
from evoagent.text2sql.agentic import Text2SQLAgenticEngine
from evoagent.text2sql.checkpoint_store import Text2SQLRuntimeCheckpointStore
from evoagent.text2sql.evolution import Text2SQLEvolutionStore
from evoagent.text2sql.memory_service import finalize_run
from evoagent.text2sql.shadow import Text2SQLShadowReleaseManager
from evoagent.text2sql.vanna_retriever import VannaRetrieverOnly


def _project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _lane_task_id(task_id: str, lane: str, policy_version: str) -> str:
    """Return a checkpoint key isolated by external request, lane, and policy."""

    if lane not in {"stable", "candidate"}:
        raise ValueError("unknown Text2SQL release lane: %s" % lane)
    return "%s:%s:%s" % (task_id, lane, policy_version)


def _failed_cli_result(
    question: str,
    task_id: str,
    stage: str,
    exc: BaseException,
    version_pins: dict[str, str],
) -> dict[str, object]:
    """Build a bounded failure Trace without persisting provider error payloads."""

    error_category = type(exc).__name__
    diagnostic = {
        "last_node": stage,
        "error_category": error_category,
    }
    return {
        "task_id": task_id,
        "status": "error",
        "question": question,
        "standalone_question": question,
        "query_type": "DATA_QUERY",
        "final_sql": "",
        "gates": {
            "accepted": False,
            "errors": ["cli_runtime_failure"],
        },
        "version_pins": dict(version_pins),
        "execution": dict(diagnostic),
        "collaboration": {"diagnostic": dict(diagnostic)},
        "answer": {
            "columns": [],
            "rows": [],
            "row_count": 0,
            "truncated": False,
            "summary_text": "",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("question")
    parser.add_argument("--principal", action="append", default=["local-user"])
    parser.add_argument(
        "--database",
        type=Path,
        default=_project_path(
            os.getenv("EVOAGENT_TEXT2SQL_SQLITE_PATH", "database/evo_text2sql_eval.sqlite3")
        ),
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=_project_path(
            os.getenv(
                "EVOAGENT_TEXT2SQL_SCHEMA_SNAPSHOT",
                "artifacts/text2sql/schema/database_snapshot.json",
            )
        ),
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
        "--checkpoint-store",
        type=Path,
        default=_project_path(
            os.getenv(
                "EVOAGENT_TEXT2SQL_CHECKPOINT_STORE",
                "artifacts/text2sql/checkpoints/runtime.sqlite3",
            )
        ),
        help="Durable SQLite store for 11-node runtime checkpoints.",
    )
    parser.add_argument("--max-rows", type=int, default=200)
    parser.add_argument("--task-id", default="")
    parser.add_argument("--session-id", default="cli")
    args = parser.parse_args()

    settings = Settings.from_env()
    llm = settings.resolved_llm()
    if not llm:
        parser.error(
            "configure an LLM with EVOAGENT_LLM_PROVIDER and its API key before running Text2SQL"
        )
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    client = JsonChatClient(
        str(llm["base_url"]),
        str(llm["api_key"]),
        str(llm["model"]),
        provider=str(llm["provider"]),
        timeout=settings.agent_time_budget_seconds,
        extra_headers=dict(llm.get("headers") or {}),
    )
    checkpoint_store = Text2SQLRuntimeCheckpointStore(args.checkpoint_store)
    vanna_index_version = VannaRetrieverOnly.current_index_version(
        args.vanna_index_root
    )
    with Text2SQLEvolutionStore(args.evolution_store, snapshot) as evolution:
        task_id = str(args.task_id or "").strip() or "text2sql-%s" % uuid.uuid4().hex
        memory_bundle = evolution.runtime_memory_snapshot()
        active_policy_version = evolution.active_policy_version
        available_version_pins = {
            key: value
            for key, value in {
                "database_snapshot_id": str(snapshot.get("snapshot_id") or ""),
                "wiki_index_version": str(vanna_index_version or ""),
                "vanna_index_version": str(vanna_index_version or ""),
                "memory_snapshot_id": str(
                    memory_bundle.get("memory_snapshot_id") or ""
                ),
                "policy_version": str(active_policy_version or ""),
            }.items()
            if value
        }

        def engine_for(version):
            policy = evolution.get_policy(version)
            return Text2SQLAgenticEngine(
                client=client,
                database_path=args.database,
                snapshot=snapshot,

                vanna_index_root=args.vanna_index_root,
                vanna_index_version=vanna_index_version,
                principals=args.principal,
                memory_snapshot_id=str(memory_bundle["memory_snapshot_id"]),
                policy_version=policy.version,
                policy_artifact=policy,
                policy_source_memory_ids=evolution.policy_source_memory_ids(
                    policy.version
                ),
                memory_snapshot_bundle=memory_bundle,
                checkpoint_store=checkpoint_store,
                token_budget=settings.agent_token_budget,
                time_budget=settings.agent_time_budget_seconds,
                max_rows=args.max_rows,
            )

        failure_stage = "cli-engine-construction"
        try:
            stable_engine = engine_for(active_policy_version)
            available_version_pins = dict(stable_engine.version_pins)
            release = Text2SQLShadowReleaseManager(evolution)

            def candidate_runner(version):
                candidate_engine = engine_for(version)
                return lambda question: candidate_engine.run(
                    question,
                    task_id=_lane_task_id(
                        task_id,
                        "candidate",
                        candidate_engine.policy_version,
                    ),
                )

            failure_stage = "cli-release-execution"
            result = release.execute(
                args.question,
                task_id,
                lambda question: stable_engine.run(
                    question,
                    task_id=_lane_task_id(
                        task_id,
                        "stable",
                        stable_engine.policy_version,
                    ),
                ),
                candidate_runner,
                stable_engine.version_pins,
                {
                    "model": {
                        "provider": str(getattr(client, "provider", "unknown")),
                        "model": str(getattr(client, "model", "unknown")),
                        "temperature": 0,
                    },
                    "runtime": dict(stable_engine.runtime_identity),
                    "principals": sorted(set(args.principal)),
                },
            )
        except Exception as exc:
            failed_result = _failed_cli_result(
                args.question,
                task_id,
                failure_stage,
                exc,
                available_version_pins,
            )
            try:
                finalize_run(
                    failed_result,
                    failed_result,
                    store=evolution,
                    task_id=task_id,
                    user_id=str(
                        args.principal[0] if args.principal else "local-user"
                    ),
                    session_id=str(args.session_id or "cli")[:200],
                    origin="cli",
                    source_lane="stable",
                )
            except Exception:
                # Failure recording is a best-effort side effect. The original
                # engine/release exception remains the CLI's primary failure.
                pass
            raise
        release_state = dict(result.get("release") or {})
        if release_state.get("shadow_sampled"):
            source_lane = (
                "candidate"
                if release_state.get("candidate_output_used")
                or str(release_state.get("lane") or "").casefold() == "canary"
                else "shadow"
            )
        else:
            source_lane = "stable"
        memory_status = finalize_run(
            {**dict(result), "task_id": task_id},
            result,
            store=evolution,
            task_id=task_id,
            user_id=str(args.principal[0] if args.principal else "local-user"),
            session_id=str(args.session_id or "cli")[:200],
            origin="cli",
            source_lane=source_lane,
        )
    result = {
        **dict(result),
        "task_id": task_id,
        **dict(memory_status.as_dict()),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result["status"] == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
