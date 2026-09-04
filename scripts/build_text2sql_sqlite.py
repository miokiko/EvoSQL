#!/usr/bin/env python3
"""Build the local read-only SQLite Text2SQL evaluation database."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evoagent.text2sql.sqlite_database import build_sqlite_database


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dump",
        type=Path,
        default=PROJECT_ROOT / "database" / "test1_full_20241118.sql",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "database" / "evo_text2sql_eval.sqlite3",
    )
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = build_sqlite_database(args.dump, args.output, replace=args.replace)
    print(
        json.dumps(
            {
                "database": str(result.database_path),
                "manifest": str(result.manifest_path),
                "database_snapshot_id": result.database_snapshot_id,
                "table_count": result.table_count,
                "row_count": result.row_count,
                "sqlite_sha256": result.database_sha256,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
