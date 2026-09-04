#!/usr/bin/env python3
"""List, approve or reject Text2SQL candidate knowledge."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evoagent.text2sql.knowledge_store import KnowledgeStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("list", "approve", "reject"))
    parser.add_argument("evidence_id", nargs="?")
    parser.add_argument("--reviewer", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument(
        "--store",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "text2sql" / "knowledge" / "knowledge.sqlite3",
    )
    args = parser.parse_args()
    with KnowledgeStore(args.store) as store:
        if args.action == "list":
            result = store.candidates()
        else:
            if not args.evidence_id:
                parser.error("evidence_id is required for approve/reject")
            version = store.review(
                args.evidence_id,
                "approve" if args.action == "approve" else "reject",
                args.reviewer,
                args.reason,
            )
            result = {"evidence_id": args.evidence_id, "stable_index_version": version}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
