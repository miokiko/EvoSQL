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
from evoagent.text2sql.shadow import Text2SQLShadowReleaseManager


def _project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _lane_task_id(task_id: str, lane: str, policy_version: str) -> str:
    """Return a checkpoint key isolated by external request, lane, and policy."""

    if lane not in {"stable", "candidate"}:
        raise ValueError("unknown Text2SQL release lane: %s" % lane)
    return "%s:%s:%s" % (task_id, lane, policy_version)


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
        "--knowledge-store",
        type=Path,
        default=_project_path(
            os.getenv(
                "EVOAGENT_TEXT2SQL_KNOWLEDGE_STORE",
                "artifacts/text2sql/knowledge/knowledge.sqlite3",
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
    with Text2SQLEvolutionStore(args.evolution_store, snapshot) as evolution:
        task_id = str(args.task_id or "").strip() or "text2sql-%s" % uuid.uuid4().hex

        def engine_for(version):
            policy = evolution.get_policy(version)
            return Text2SQLAgenticEngine(
                client=client,
                database_path=args.database,
                snapshot=snapshot,
                knowledge_store_path=args.knowledge_store,
                principals=args.principal,
                memory_snapshot_id=evolution.memory_snapshot_id,
                policy_version=policy.version,
                policy_artifact=policy,
                stable_memory_provider=evolution.stable_memory,
                checkpoint_store=checkpoint_store,
                token_budget=settings.agent_token_budget,
                time_budget=settings.agent_time_budget_seconds,
                max_rows=args.max_rows,
            )

        stable_engine = engine_for(evolution.active_policy_version)
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
        )
    result = {**dict(result), "task_id": task_id}
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result["status"] == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
