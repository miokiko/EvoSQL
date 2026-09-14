#!/usr/bin/env python3
"""Build the single-user Vanna corpus from local trusted sources."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evoagent.text2sql.vanna_corpus import (
    build_vanna_corpus,
    question_sql_registry_path,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Index schema, business Markdown and user-confirmed Question-SQL "
            "directly in Vanna/Chroma."
        )
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=PROJECT_ROOT
        / "artifacts"
        / "text2sql"
        / "schema"
        / "database_snapshot.json",
    )
    parser.add_argument(
        "--business-root",
        type=Path,
        default=PROJECT_ROOT / "knowledge" / "business",
    )
    parser.add_argument(
        "--join-catalog",
        type=Path,
        default=PROJECT_ROOT
        / "artifacts"
        / "text2sql"
        / "schema"
        / "join_catalog.review.json",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "text2sql" / "vanna",
    )
    parser.add_argument(
        "--question-sql",
        type=Path,
        default=None,
        help="Optional confirmed Question-SQL registry; defaults inside --root.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    join_catalog = (
        json.loads(args.join_catalog.read_text(encoding="utf-8"))
        if args.join_catalog.exists()
        else None
    )
    result = build_vanna_corpus(
        args.root,
        snapshot,
        business_root=args.business_root,
        join_catalog=join_catalog,
        question_sql_path=args.question_sql or question_sql_registry_path(args.root),
        enabled=True,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
