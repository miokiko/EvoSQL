#!/usr/bin/env python3
"""Create the Text2SQL evolution control-plane store and empty baseline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evoagent.text2sql.evolution import Text2SQLEvolutionStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "text2sql" / "schema" / "database_snapshot.json",
    )
    parser.add_argument(
        "--store",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "text2sql" / "evolution" / "evolution.sqlite3",
    )
    args = parser.parse_args()
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    with Text2SQLEvolutionStore(args.store, snapshot) as store:
        output = {
            "store": str(args.store.resolve()),
            "database_snapshot_id": snapshot["snapshot_id"],
            "active_policy_version": store.active_policy_version,
            "memory_snapshot_id": store.memory_snapshot_id,
            "policies": store.list_policies(),
        }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
