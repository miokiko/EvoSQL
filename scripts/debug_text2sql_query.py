#!/usr/bin/env python3
"""Run one local Text2SQL query and print bounded collaboration diagnostics."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evoagent.config import Settings
from evoagent.llm import JsonChatClient
from evoagent.text2sql.agentic import Text2SQLAgenticEngine
from evoagent.text2sql.evolution import Text2SQLEvolutionStore
from evoagent.text2sql.memory_service import finalize_run
from evoagent.text2sql.vanna_retriever import VannaRetrieverOnly


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("question")
    args = parser.parse_args()
    settings = Settings.from_env()
    llm = settings.resolved_llm()
    snapshot = json.loads(
        (PROJECT_ROOT / "artifacts/text2sql/schema/database_snapshot.json").read_text(
            encoding="utf-8"
        )
    )
    with Text2SQLEvolutionStore(
        PROJECT_ROOT / "artifacts/text2sql/evolution/evolution.sqlite3", snapshot
    ) as evolution:
        policy = evolution.get_policy()
        memory_bundle = evolution.runtime_memory_snapshot()
        vanna_root = PROJECT_ROOT / "artifacts/text2sql/vanna"
        vanna_version = VannaRetrieverOnly.current_index_version(vanna_root)
        engine = Text2SQLAgenticEngine(
            client=JsonChatClient(
                str(llm["base_url"]),
                str(llm["api_key"]),
                str(llm["model"]),
                provider=str(llm["provider"]),
                timeout=settings.agent_time_budget_seconds,
                extra_headers=dict(llm.get("headers") or {}),
            ),
            database_path=PROJECT_ROOT / "database/evo_text2sql_eval.sqlite3",
            snapshot=snapshot,
            vanna_index_root=vanna_root,
            vanna_index_version=vanna_version,
            principals=["local-user"],
            memory_snapshot_id=str(memory_bundle["memory_snapshot_id"]),
            policy_version=policy.version,
            policy_artifact=policy,
            policy_source_memory_ids=evolution.policy_source_memory_ids(
                policy.version
            ),
            memory_snapshot_bundle=memory_bundle,
            token_budget=settings.agent_token_budget,
            time_budget=settings.agent_time_budget_seconds,
        )
        result = engine.run(args.question)
        memory_status = finalize_run(
            result,
            result,
            store=evolution,
            task_id=str(result.get("task_id") or ""),
            origin="debug",
            source_lane="stable",
            session_id="debug",
        )
    workers = result.get("collaboration", {}).get("worker_results", [])
    diagnostic = {
        "status": result.get("status"),
        "final_sql": result.get("final_sql"),
        "gates": result.get("gates"),
        "draft_link_pack": result.get("collaboration", {}).get("draft_link_pack", {}),
        "workers": [
            {
                "worker": item.get("worker"),
                "status": item.get("status"),
                "error": item.get("error"),
                "output": item.get("output"),
            }
            for item in workers
        ],
        "usage": {
            key: result.get("execution", {}).get(key)
            for key in ("llm_calls", "input_tokens", "output_tokens", "total_tokens")
        },
        "memory": dict(memory_status.as_dict()),
    }
    print(json.dumps(diagnostic, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
