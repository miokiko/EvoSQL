#!/usr/bin/env python3
"""Generate and execute-validate the independent 240-case Text2SQL dataset."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evoagent.text2sql.dataset_builder import build_dataset


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "text2sql" / "schema" / "database_snapshot.json",
    )
    parser.add_argument(
        "--join-catalog",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "text2sql" / "schema" / "join_catalog.review.json",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=PROJECT_ROOT / "database" / "evo_text2sql_eval.sqlite3",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "evaluation" / "datasets" / "text2sql_v1",
    )
    args = parser.parse_args()
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    joins = json.loads(args.join_catalog.read_text(encoding="utf-8"))
    manifest = build_dataset(snapshot, joins, args.database, args.output)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
