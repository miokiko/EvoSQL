"""Deterministic, read-only SQLite evaluation database for Text2SQL."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .schema_catalog import (
    _CREATE_TABLE,
    _INSERT,
    _decode_value,
    _parse_table,
    _split_values,
    build_snapshot_from_dump,
    sha256_file,
)


_INTEGER_TYPES = {"bigint", "int", "integer", "mediumint", "smallint", "tinyint"}
_REAL_TYPES = {"decimal", "double", "float", "numeric", "real"}
_BLOB_TYPES = {"binary", "blob", "longblob", "mediumblob", "tinyblob", "varbinary"}


@dataclass(frozen=True)
class SQLiteBuildResult:
    database_path: Path
    manifest_path: Path
    database_snapshot_id: str
    table_count: int
    row_count: int
    database_sha256: str


def _quote_identifier(value: str) -> str:
    return '"%s"' % value.replace('"', '""')


def _sqlite_type(mysql_type: str) -> str:
    if mysql_type in _INTEGER_TYPES:
        return "INTEGER"
    if mysql_type in _REAL_TYPES:
        return "REAL"
    if mysql_type in _BLOB_TYPES:
        return "BLOB"
    return "TEXT"


def _decode_sqlite_value(token: str, mysql_type: str) -> Any:
    value = _decode_value(token)
    if value is None:
        return None
    if mysql_type in _BLOB_TYPES:
        marker = str(value)
        if marker.lower().startswith("0x") and re.fullmatch(r"0x[0-9A-Fa-f]*", marker):
            return bytes.fromhex(marker[2:])
        if isinstance(value, str):
            return value.encode("utf-8")
        return value
    if mysql_type in _INTEGER_TYPES:
        return int(str(value))
    if mysql_type in _REAL_TYPES:
        return float(str(value))
    return value


def _create_table_sql(table: Mapping[str, Any]) -> str:
    definitions: list[str] = []
    for column in table["columns"]:
        definition = "%s %s" % (
            _quote_identifier(column["name"]),
            _sqlite_type(column["data_type"]),
        )
        if not column["nullable"]:
            definition += " NOT NULL"
        definitions.append(definition)
    if table["primary_key"]:
        definitions.append(
            "PRIMARY KEY (%s)"
            % ", ".join(_quote_identifier(name) for name in table["primary_key"])
        )
    return "CREATE TABLE %s (\n  %s\n)" % (
        _quote_identifier(table["name"]),
        ",\n  ".join(definitions),
    )


def open_readonly(path: Path) -> sqlite3.Connection:
    """Open a SQLite database in OS-level read-only and SQLite query-only mode."""

    resolved = path.resolve()
    connection = sqlite3.connect("file:%s?mode=ro&immutable=1" % resolved, uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


def _verify_readonly(path: Path, expected: Mapping[str, Any]) -> tuple[int, int]:
    connection = open_readonly(path)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError("SQLite integrity check failed: %s" % integrity)
        actual_tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        expected_tables = [table["name"] for table in expected["tables"]]
        if actual_tables != expected_tables:
            raise RuntimeError("SQLite table set does not match the source snapshot")
        row_count = 0
        for table in expected["tables"]:
            actual = int(
                connection.execute(
                    "SELECT COUNT(*) FROM %s" % _quote_identifier(table["name"])
                ).fetchone()[0]
            )
            if actual != table["row_count"]:
                raise RuntimeError(
                    "row count mismatch for %s: expected %d, got %d"
                    % (table["name"], table["row_count"], actual)
                )
            row_count += actual
        try:
            connection.execute("CREATE TABLE __readonly_probe(value INTEGER)")
        except sqlite3.OperationalError:
            pass
        else:
            raise RuntimeError("SQLite read-only connection unexpectedly accepted DDL")
        return len(actual_tables), row_count
    finally:
        connection.close()


def build_sqlite_database(
    dump_path: Path,
    output_path: Path,
    manifest_path: Path | None = None,
    replace: bool = False,
) -> SQLiteBuildResult:
    """Convert the copied MySQL dump into an isolated SQLite database."""

    dump_path = dump_path.resolve()
    output_path = output_path.resolve()
    if output_path.exists() and not replace:
        raise FileExistsError("refusing to replace existing database: %s" % output_path)
    sql = dump_path.read_text(encoding="utf-8-sig")
    tables = [_parse_table(match) for match in _CREATE_TABLE.finditer(sql)]
    tables.sort(key=lambda item: item["name"])
    if not tables:
        raise ValueError("no tables found in %s" % dump_path)
    by_name = {table["name"]: table for table in tables}

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(output_path.name + ".building")
    if temporary.exists():
        temporary.unlink()
    connection = sqlite3.connect(temporary)
    imported_rows = 0
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA application_id = 1163284817")
        connection.execute("PRAGMA user_version = 1")
        for table in tables:
            connection.execute(_create_table_sql(table))
        for match in _INSERT.finditer(sql):
            table = by_name.get(match.group("table"))
            if table is None:
                continue
            tokens = _split_values(match.group("values"))
            if len(tokens) != len(table["columns"]):
                raise ValueError("INSERT column count mismatch for %s" % table["name"])
            values = [
                _decode_sqlite_value(token, column["data_type"])
                for token, column in zip(tokens, table["columns"])
            ]
            placeholders = ", ".join("?" for _ in values)
            connection.execute(
                "INSERT INTO %s VALUES (%s)"
                % (_quote_identifier(table["name"]), placeholders),
                values,
            )
            imported_rows += 1
        for table in tables:
            for index in table["indexes"]:
                if index["columns"] == table["primary_key"]:
                    continue
                index_name = "%s__%s" % (table["name"], index["name"])
                connection.execute(
                    "CREATE %s INDEX %s ON %s (%s)"
                    % (
                        "UNIQUE" if index["unique"] else "",
                        _quote_identifier(index_name),
                        _quote_identifier(table["name"]),
                        ", ".join(_quote_identifier(name) for name in index["columns"]),
                    )
                )
        connection.commit()
    except Exception:
        connection.close()
        temporary.unlink(missing_ok=True)
        raise
    finally:
        try:
            connection.close()
        except Exception:
            pass

    source_snapshot = build_snapshot_from_dump(dump_path)
    os.chmod(temporary, 0o444)
    try:
        table_count, verified_rows = _verify_readonly(temporary, source_snapshot.snapshot)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    if imported_rows != verified_rows:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("imported row count does not match verified row count")
    os.replace(temporary, output_path)
    database_sha256 = sha256_file(output_path)
    manifest_path = (
        manifest_path.resolve()
        if manifest_path
        else output_path.with_name(output_path.name + ".manifest.json")
    )
    manifest = {
        "contract_version": 1,
        "dialect": "sqlite",
        "database_snapshot_id": source_snapshot.snapshot["snapshot_id"],
        "source_dump_sha256": source_snapshot.snapshot["source"]["dump_sha256"],
        "sqlite_sha256": database_sha256,
        "sqlite_version": sqlite3.sqlite_version,
        "table_count": table_count,
        "row_count": verified_rows,
        "open_mode": "file:<path>?mode=ro&immutable=1 + PRAGMA query_only=ON",
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return SQLiteBuildResult(
        database_path=output_path,
        manifest_path=manifest_path,
        database_snapshot_id=source_snapshot.snapshot["snapshot_id"],
        table_count=table_count,
        row_count=verified_rows,
        database_sha256=database_sha256,
    )
