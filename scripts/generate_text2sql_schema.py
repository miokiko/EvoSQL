#!/usr/bin/env python3
"""Generate deterministic Text2SQL cold-start artifacts from the copied dump."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evoagent.text2sql.schema_catalog import build_snapshot_from_dump, write_snapshot_artifacts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dump",
        type=Path,
        default=PROJECT_ROOT / "database" / "test1_full_20241118.sql",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "text2sql" / "schema",
    )
    parser.add_argument("--database", default="evo_text2sql_eval")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    artifacts = build_snapshot_from_dump(args.dump, database_name=args.database)
    paths = write_snapshot_artifacts(artifacts, args.output_dir)
    print(
        json.dumps(
            {
                "snapshot_id": artifacts.snapshot["snapshot_id"],
                "table_count": artifacts.snapshot["table_count"],
                "row_count": artifacts.snapshot["row_count"],
                "join_candidates": len(artifacts.join_candidates),
                "files": {name: str(path) for name, path in paths.items()},
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
