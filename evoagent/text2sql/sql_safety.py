"""AST safety, EXPLAIN and bounded read-only execution for SQLite Text2SQL."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import sqlglot
from sqlglot import exp

from .contracts import SQLExecutionResult, SQLGateResult
from .sqlite_database import open_readonly


_BLOCKED_NODES = (
    exp.Alter,
    exp.Command,
    exp.Create,
    exp.Delete,
    exp.Drop,
    exp.Insert,
    exp.Merge,
    exp.Transaction,
    exp.Update,
    exp.Use,
)
_BLOCKED_FUNCTIONS = {"load_extension", "readfile", "writefile"}
_COMMENT = re.compile(r"--|/\*|\*/")


def _schema_lookup(snapshot: Mapping[str, Any]) -> tuple[set[str], Mapping[str, set[str]]]:
    columns = {
        table["name"]: {column["name"] for column in table["columns"]}
        for table in snapshot["tables"]
    }
    return set(columns), columns


def validate_sql(sql: str, snapshot: Mapping[str, Any]) -> SQLGateResult:
    errors: list[str] = []
    if not isinstance(sql, str) or not sql.strip():
        return SQLGateResult(False, errors=("empty_sql",))
    if len(sql.encode("utf-8")) > 64_000:
        return SQLGateResult(False, errors=("sql_too_large",))
    if _COMMENT.search(sql):
        errors.append("comments_not_allowed")
    try:
        statements = [item for item in sqlglot.parse(sql, read="sqlite") if item is not None]
    except sqlglot.errors.ParseError:
        return SQLGateResult(False, errors=tuple(errors + ["parse_error"]))
    if len(statements) != 1:
        return SQLGateResult(False, errors=tuple(errors + ["exactly_one_statement_required"]))
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        errors.append("read_only_query_required")
    if any(tree.find(node_type) is not None for node_type in _BLOCKED_NODES):
        errors.append("write_or_control_statement_forbidden")
    blocked_functions = {
        str(node.name).lower()
        for node in tree.find_all(exp.Func)
        if str(node.name).lower() in _BLOCKED_FUNCTIONS
    }
    if blocked_functions:
        errors.append("blocked_function:%s" % ",".join(sorted(blocked_functions)))

    allowed_tables, allowed_columns = _schema_lookup(snapshot)
    virtual_names = {
        node.alias_or_name
        for node in (*tuple(tree.find_all(exp.CTE)), *tuple(tree.find_all(exp.Subquery)))
        if node.alias_or_name
    }
    table_nodes = [
        table for table in tree.find_all(exp.Table) if table.name not in virtual_names
    ]
    tables = tuple(sorted({table.name for table in table_nodes}))
    aliases = {
        table.alias_or_name: table.name for table in table_nodes if table.alias_or_name
    }
    for table in tables:
        if table not in allowed_tables:
            errors.append("unknown_table:%s" % table)

    columns: set[str] = set()
    referenced_tables = {table for table in tables if table in allowed_tables}
    for column in tree.find_all(exp.Column):
        if isinstance(column.this, exp.Star):
            continue
        name = column.name
        qualifier = column.table
        if qualifier:
            if qualifier in virtual_names:
                columns.add("%s.%s" % (qualifier, name))
                continue
            table_name = aliases.get(qualifier, qualifier)
            qualified = "%s.%s" % (table_name, name)
            columns.add(qualified)
            if table_name not in allowed_columns or name not in allowed_columns[table_name]:
                errors.append("unknown_column:%s" % qualified)
        else:
            columns.add(name)
            if referenced_tables and not any(
                name in allowed_columns[table] for table in referenced_tables
            ):
                errors.append("unknown_column:%s" % name)

    normalized = tree.sql(dialect="sqlite", pretty=False)
    fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return SQLGateResult(
        not errors,
        normalized,
        tables,
        tuple(sorted(columns)),
        tuple(dict.fromkeys(errors)),
        fingerprint,
    )


class ReadOnlySQLiteExecutor:
    def __init__(
        self,
        database_path: Path,
        snapshot: Mapping[str, Any],
        max_rows: int = 200,
        timeout_ms: int = 3000,
        progress_steps: int = 1000,
    ) -> None:
        self.database_path = database_path.resolve()
        self.snapshot = snapshot
        self.max_rows = max(1, min(int(max_rows), 10_000))
        self.timeout_ms = max(10, min(int(timeout_ms), 60_000))
        self.progress_steps = max(10, int(progress_steps))

    def _checked(self, sql: str) -> SQLGateResult:
        result = validate_sql(sql, self.snapshot)
        if not result.accepted:
            raise ValueError("SQL safety gate rejected query: %s" % ", ".join(result.errors))
        return result

    def explain(self, sql: str) -> Sequence[Sequence[Any]]:
        checked = self._checked(sql)
        connection = open_readonly(self.database_path)
        try:
            return tuple(
                tuple(row)
                for row in connection.execute(
                    "EXPLAIN QUERY PLAN " + checked.normalized_sql
                ).fetchall()
            )
        finally:
            connection.close()

    def execute(self, sql: str) -> SQLExecutionResult:
        checked = self._checked(sql)
        connection = open_readonly(self.database_path)
        started = time.monotonic()

        def progress() -> int:
            return int((time.monotonic() - started) * 1000 >= self.timeout_ms)

        connection.set_progress_handler(progress, self.progress_steps)
        try:
            explain = tuple(
                tuple(row)
                for row in connection.execute(
                    "EXPLAIN QUERY PLAN " + checked.normalized_sql
                ).fetchall()
            )
            cursor = connection.execute(checked.normalized_sql)
            columns = tuple(item[0] for item in (cursor.description or ()))
            values = cursor.fetchmany(self.max_rows + 1)
            truncated = len(values) > self.max_rows
            rows = tuple(tuple(value for value in row) for row in values[: self.max_rows])
            return SQLExecutionResult(
                columns=columns,
                rows=rows,
                row_count=len(rows),
                truncated=truncated,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                explain_plan=explain,
                sql_fingerprint=checked.fingerprint,
            )
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).lower():
                raise TimeoutError("SQLite query exceeded execution budget") from exc
            raise
        finally:
            connection.set_progress_handler(None, 0)
            connection.close()
