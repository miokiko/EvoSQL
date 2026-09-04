#!/usr/bin/env python3
"""Build an immutable retriever-only Vanna index from stable knowledge."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evoagent.text2sql.knowledge_store import KnowledgeStore
from evoagent.text2sql.vanna_retriever import VannaRetrieverOnly


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--store",
        type=Path,
        default=PROJECT_ROOT
        / "artifacts"
        / "text2sql"
        / "knowledge"
        / "knowledge.sqlite3",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "text2sql" / "vanna",
    )
    args = parser.parse_args()
    with KnowledgeStore(args.store) as store:
        index_version = store.current_index_version("stable")
        snapshot_id = store.database_snapshot_id()
        stable_items = store.stable_items_for_index()
    retriever = VannaRetrieverOnly(args.root, index_version, enabled=True)
    result = retriever.build(stable_items, snapshot_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
