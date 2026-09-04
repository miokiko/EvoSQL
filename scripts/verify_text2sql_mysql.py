#!/usr/bin/env python3
"""Verify that the isolated MySQL import matches the pinned dump snapshot."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")
_WRITE_PRIVILEGES = {
    "ALTER",
    "CREATE",
    "DELETE",
    "DROP",
    "EVENT",
    "EXECUTE",
    "INDEX",
    "INSERT",
    "REFERENCES",
    "TRIGGER",
    "UPDATE",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.getenv("EVOAGENT_TEXT2SQL_DB_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("EVOAGENT_TEXT2SQL_DB_PORT", "3307"))
    )
    parser.add_argument("--user", default=os.getenv("EVOAGENT_TEXT2SQL_DB_USER", "evo_text2sql_ro"))
    parser.add_argument("--password", default=os.getenv("EVOAGENT_TEXT2SQL_DB_PASSWORD", ""))
    parser.add_argument(
        "--database", default=os.getenv("EVOAGENT_TEXT2SQL_DB_NAME", "evo_text2sql_eval")
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "text2sql" / "schema" / "database_snapshot.json",
    )
    return parser.parse_args()


def _quote_identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError("unsafe MySQL identifier: %s" % value)
    return "`%s`" % value


def _write_privileges(grants: list[str]) -> list[str]:
    found: set[str] = set()
    for grant in grants:
        upper = grant.upper()
        if "ALL PRIVILEGES" in upper:
            found.add("ALL PRIVILEGES")
        for privilege in _WRITE_PRIVILEGES:
            if re.search(r"(?:GRANT|,)\s+[^\n]*\b%s\b" % privilege, upper):
                found.add(privilege)
    return sorted(found)


def main() -> int:
    args = parse_args()
    if not args.password:
        raise ValueError("EVOAGENT_TEXT2SQL_DB_PASSWORD or --password is required")
    expected: dict[str, Any] = json.loads(args.snapshot.read_text(encoding="utf-8"))
    if expected["database"] != args.database:
        raise ValueError(
            "snapshot database %s does not match requested database %s"
            % (expected["database"], args.database)
        )
    try:
        import mysql.connector
    except ImportError as exc:
        raise RuntimeError("install requirements.txt before verifying MySQL") from exc

    connection = mysql.connector.connect(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=args.database,
        connection_timeout=10,
    )
    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME",
            (args.database,),
        )
        actual_tables = [row[0] for row in cursor.fetchall()]
        expected_tables = [table["name"] for table in expected["tables"]]
        if actual_tables != expected_tables:
            raise RuntimeError(
                "table mismatch: missing=%s unexpected=%s"
                % (
                    sorted(set(expected_tables) - set(actual_tables)),
                    sorted(set(actual_tables) - set(expected_tables)),
                )
            )

        actual_row_count = 0
        row_counts: dict[str, int] = {}
        for table in expected["tables"]:
            table_name = table["name"]
            cursor.execute("SELECT COUNT(*) FROM %s" % _quote_identifier(table_name))
            row_count = int(cursor.fetchone()[0])
            row_counts[table_name] = row_count
            actual_row_count += row_count
            if row_count != table["row_count"]:
                raise RuntimeError(
                    "row count mismatch for %s: expected %d, got %d"
                    % (table_name, table["row_count"], row_count)
                )

        cursor.execute("SHOW GRANTS FOR CURRENT_USER()")
        grants = [row[0] for row in cursor.fetchall()]
        unexpected = _write_privileges(grants)
        if unexpected:
            raise RuntimeError("read-only account has write privileges: %s" % unexpected)
        cursor.close()
    finally:
        connection.close()

    print(
        json.dumps(
            {
                "status": "verified",
                "database_snapshot_id": expected["snapshot_id"],
                "database": args.database,
                "table_count": len(actual_tables),
                "row_count": actual_row_count,
                "grants": grants,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
