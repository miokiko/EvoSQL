#!/usr/bin/env python3
"""Prepare one Navicat SQL file that is hard-bound to the isolated eval DB."""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")


def _literal(value: str) -> str:
    return "'%s'" % value.replace("\\", "\\\\").replace("'", "\\'")


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
        default=PROJECT_ROOT / "database" / "evo_text2sql_eval.local.sql",
    )
    parser.add_argument("--database", default="evo_text2sql_eval")
    parser.add_argument("--readonly-user", default="evo_text2sql_ro")
    parser.add_argument(
        "--readonly-password",
        default=os.getenv(
            "EVOAGENT_TEXT2SQL_DB_PASSWORD", "evo-text2sql-local-readonly"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not _IDENTIFIER.fullmatch(args.database):
        raise ValueError("database must contain only letters, digits and underscore")
    if not _IDENTIFIER.fullmatch(args.readonly_user):
        raise ValueError("readonly user must contain only letters, digits and underscore")
    if not args.readonly_password:
        raise ValueError("readonly password cannot be empty")
    dump = args.dump.read_text(encoding="utf-8-sig")
    database = "`%s`" % args.database
    user = _literal(args.readonly_user)
    password = _literal(args.readonly_password)
    accounts = ["%s@'localhost'" % user, "%s@'127.0.0.1'" % user]

    prefix = """-- Generated for Navicat. Every DROP TABLE in the source dump is scoped
-- to the new eval database by the USE statement below.
CREATE DATABASE IF NOT EXISTS {database}
  CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE {database};

""".format(database=database)
    suffix_lines = [
        "",
        "-- Replace/reset only the dedicated local application accounts.",
    ]
    for account in accounts:
        suffix_lines.extend(
            [
                "CREATE USER IF NOT EXISTS %s IDENTIFIED BY %s;" % (account, password),
                "ALTER USER %s IDENTIFIED BY %s;" % (account, password),
                "REVOKE ALL PRIVILEGES, GRANT OPTION FROM %s;" % account,
                "GRANT SELECT, SHOW VIEW ON %s.* TO %s;" % (database, account),
            ]
        )
    suffix_lines.extend(
        [
            "FLUSH PRIVILEGES;",
            "SELECT COUNT(*) AS imported_tables",
            "FROM information_schema.TABLES",
            "WHERE TABLE_SCHEMA = 'evo_text2sql_eval';",
            "SHOW GRANTS FOR 'evo_text2sql_ro'@'localhost';",
            "SHOW GRANTS FOR 'evo_text2sql_ro'@'127.0.0.1';",
            "",
        ]
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(prefix + dump.rstrip() + "\n" + "\n".join(suffix_lines), encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
