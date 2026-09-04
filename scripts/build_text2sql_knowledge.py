#!/usr/bin/env python3
"""Build/update the local Text2SQL candidate and stable knowledge indexes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evoagent.text2sql.knowledge_store import KnowledgeStore
from evoagent.text2sql.markdown_wiki import MarkdownWikiConnector
from evoagent.text2sql.schema_catalog import sha256_file


def parse_args() -> argparse.Namespace:
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
    parser.add_argument("--wiki-root", type=Path, default=PROJECT_ROOT / "knowledge" / "wiki")
    parser.add_argument(
        "--store",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "text2sql" / "knowledge" / "knowledge.sqlite3",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    join_catalog = json.loads(args.join_catalog.read_text(encoding="utf-8"))
    connector = MarkdownWikiConnector(args.wiki_root)
    with KnowledgeStore(args.store) as store:
        database_counts = store.ingest_database(snapshot, join_catalog)
        sync = store.sync_wiki("local-markdown-wiki", connector, snapshot)
        stats = store.stats()
        candidates = store.candidates()
    manifest = {
        "contract_version": 1,
        "database_snapshot_id": snapshot["snapshot_id"],
        "knowledge_store_sha256": sha256_file(args.store),
        "database_counts": database_counts,
        "sync": sync,
        "stats": stats,
        "candidate_count": len(candidates),
    }
    manifest_path = args.store.with_name("manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
