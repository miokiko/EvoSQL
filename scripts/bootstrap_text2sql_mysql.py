#!/usr/bin/env python3
"""Import the copied dump into an isolated MySQL database.

The script requires an administrative account only during bootstrap. It creates
an application account with SELECT/SHOW VIEW and prints the resulting grants.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _identifier(value: str, label: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError("%s must contain only letters, digits and underscore" % label)
    return "`%s`" % value


def _literal(value: str) -> str:
    return "'%s'" % value.replace("\\", "\\\\").replace("'", "\\'")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=_env("EVOAGENT_TEXT2SQL_DB_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(_env("EVOAGENT_TEXT2SQL_DB_PORT", "3307"))
    )
    parser.add_argument("--admin-user", default=_env("EVOAGENT_TEXT2SQL_ADMIN_USER", "root"))
    parser.add_argument(
        "--admin-password", default=_env("EVOAGENT_TEXT2SQL_ADMIN_PASSWORD", "")
    )
    parser.add_argument(
        "--database", default=_env("EVOAGENT_TEXT2SQL_DB_NAME", "evo_text2sql_eval")
    )
    parser.add_argument(
        "--readonly-user", default=_env("EVOAGENT_TEXT2SQL_DB_USER", "evo_text2sql_ro")
    )
    parser.add_argument(
        "--readonly-password", default=_env("EVOAGENT_TEXT2SQL_DB_PASSWORD", "")
    )
    parser.add_argument("--readonly-host", default="%")
    parser.add_argument(
        "--dump",
        type=Path,
        default=PROJECT_ROOT / "database" / "test1_full_20241118.sql",
    )
    parser.add_argument("--mysql-bin", default=shutil.which("mysql") or "mysql")
    return parser.parse_args()


def _connect(args: argparse.Namespace, database: str | None = None) -> Any:
    try:
        import mysql.connector
    except ImportError as exc:
        raise RuntimeError("install requirements.txt before bootstrapping MySQL") from exc
    return mysql.connector.connect(
        host=args.host,
        port=args.port,
        user=args.admin_user,
        password=args.admin_password,
        database=database,
        connection_timeout=10,
        autocommit=True,
    )


def _import_dump(args: argparse.Namespace) -> None:
    environment = os.environ.copy()
    if args.admin_password:
        environment["MYSQL_PWD"] = args.admin_password
    command = [
        args.mysql_bin,
        "--protocol=TCP",
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--user",
        args.admin_user,
        "--default-character-set=utf8mb4",
        "--database",
        args.database,
    ]
    with args.dump.open("rb") as source:
        completed = subprocess.run(
            command,
            stdin=source,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )
    if completed.returncode:
        raise RuntimeError(
            "mysql dump import failed: %s" % completed.stderr.decode("utf-8", errors="replace")
        )


def main() -> int:
    args = parse_args()
    _identifier(args.database, "database")
    _identifier(args.readonly_user, "readonly user")
    if not args.readonly_password:
        raise ValueError("EVOAGENT_TEXT2SQL_DB_PASSWORD or --readonly-password is required")
    if not args.dump.is_file():
        raise FileNotFoundError(args.dump)

    database_identifier = _identifier(args.database, "database")
    account = "%s@%s" % (_literal(args.readonly_user), _literal(args.readonly_host))
    connection = _connect(args)
    try:
        cursor = connection.cursor()
        cursor.execute(
            "CREATE DATABASE IF NOT EXISTS %s CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            % database_identifier
        )
        cursor.close()
    finally:
        connection.close()

    _import_dump(args)

    connection = _connect(args)
    try:
        cursor = connection.cursor()
        cursor.execute("CREATE USER IF NOT EXISTS %s IDENTIFIED BY %s" % (account, _literal(args.readonly_password)))
        cursor.execute("ALTER USER %s IDENTIFIED BY %s" % (account, _literal(args.readonly_password)))
        cursor.execute("REVOKE ALL PRIVILEGES, GRANT OPTION FROM %s" % account)
        cursor.execute("GRANT SELECT, SHOW VIEW ON %s.* TO %s" % (database_identifier, account))
        cursor.execute("SHOW GRANTS FOR %s" % account)
        grants = [row[0] for row in cursor.fetchall()]
        cursor.execute(
            "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s",
            (args.database,),
        )
        table_count = int(cursor.fetchone()[0])
        cursor.close()
    finally:
        connection.close()

    readonly_args = argparse.Namespace(**vars(args))
    readonly_args.admin_user = args.readonly_user
    readonly_args.admin_password = args.readonly_password
    readonly_connection = _connect(readonly_args, database=args.database)
    try:
        cursor = readonly_connection.cursor()
        cursor.execute("SELECT DATABASE()")
        selected_database = cursor.fetchone()[0]
        cursor.close()
    finally:
        readonly_connection.close()

    print(
        json.dumps(
            {
                "database": selected_database,
                "table_count": table_count,
                "readonly_account": "%s@%s" % (args.readonly_user, args.readonly_host),
                "grants": grants,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
