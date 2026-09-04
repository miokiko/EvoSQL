#!/usr/bin/env python3
"""Inspect the role-specific stable EvidencePack for a natural-language query."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evoagent.text2sql.knowledge_store import KnowledgeStore, ROLE_VIEWS


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--role", choices=sorted(ROLE_VIEWS), default="schema-grounding")
    parser.add_argument("--principal", action="append", default=["local-user"])
    parser.add_argument(
        "--store",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "text2sql" / "knowledge" / "knowledge.sqlite3",
    )
    args = parser.parse_args()
    with KnowledgeStore(args.store) as store:
        pack = store.retrieve(
            args.query,
            args.role,
            args.principal,
            memory_snapshot_id="memory-empty-v1",
            policy_version="text2sql-sqlite-baseline-v1",
        )
    print(json.dumps(pack.as_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
