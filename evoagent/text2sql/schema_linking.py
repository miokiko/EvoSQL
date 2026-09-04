"""Deterministic schema expansion around an untrusted draft SQL proposal.

The draft is never executed. It is parsed only to recover schema candidates,
which are checked against the pinned snapshot and handed to Grounding.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from sqlglot import exp, parse_one


_TABLE_TOKEN = re.compile(r"\bt_[A-Za-z0-9_]+\b", re.IGNORECASE)
_QUALIFIED_TOKEN = re.compile(
    r"\b(t_[A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\b", re.IGNORECASE
)
_EXPLICIT_JOIN = re.compile(
    r"\b(t_[A-Za-z0-9_]+\.[A-Za-z0-9_]+)\s*=\s*"
    r"(t_[A-Za-z0-9_]+\.[A-Za-z0-9_]+)\b",
    re.IGNORECASE,
)
_QUOTED_VALUE = re.compile(r"[\"'“”‘’]([^\"'“”‘’]{1,120})[\"'“”‘’]")


def _unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _table_map(snapshot: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(table["name"]): table
        for table in snapshot.get("tables") or ()
        if isinstance(table, Mapping) and table.get("name")
    }


def _column_map(table: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(column["name"]): column
        for column in table.get("columns") or ()
        if isinstance(column, Mapping) and column.get("name")
    }


def _canonical(value: str, candidates: Sequence[str]) -> str:
    return {candidate.lower(): candidate for candidate in candidates}.get(value.lower(), "")


def _resolve_column(
    table_name: str,
    column_name: str,
    tables: Mapping[str, Mapping[str, Any]],
) -> str:
    table_name = _canonical(table_name, tuple(tables))
    if not table_name:
        return ""
    column_name = _canonical(column_name, tuple(_column_map(tables[table_name])))
    return "%s.%s" % (table_name, column_name) if column_name else ""


def _explicit_joins(
    question: str,
    tables: Mapping[str, Mapping[str, Any]],
) -> list[Mapping[str, str]]:
    joins = []
    for left_token, right_token in _EXPLICIT_JOIN.findall(question or ""):
        left_table, left_column = left_token.split(".", 1)
        right_table, right_column = right_token.split(".", 1)
        left = _resolve_column(left_table, left_column, tables)
        right = _resolve_column(right_table, right_column, tables)
        if left and right and left.split(".", 1)[0] != right.split(".", 1)[0]:
            joins.append(
                {
                    "left": left,
                    "right": right,
                    "type": "inner",
                    "source": "user_explicit",
                    "evidence_id": "",
                }
            )
    return joins


def parse_draft_sql(
    draft_sql: str,
    question: str,
    snapshot: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Parse an untrusted SELECT and return only snapshot-authorized links."""

    empty = {
        "valid": False,
        "tables": [],
        "columns": [],
        "projection_columns": [],
        "joins": [],
        "error": "",
    }
    if not str(draft_sql or "").strip():
        return dict(empty, error="empty_draft_sql")
    try:
        tree = parse_one(draft_sql, read="sqlite")
    except Exception as exc:
        return dict(empty, error="parse_error:%s" % str(exc)[:200])
    if not isinstance(tree, exp.Query) or any(
        tree.find(kind) for kind in (exp.Insert, exp.Update, exp.Delete)
    ):
        return dict(empty, error="draft_is_not_select")

    tables = _table_map(snapshot)
    aliases: dict[str, str] = {}
    selected_tables: list[str] = []
    for node in tree.find_all(exp.Table):
        canonical = _canonical(node.name, tuple(tables))
        if not canonical:
            continue
        selected_tables.append(canonical)
        aliases[canonical.lower()] = canonical
        if node.alias:
            aliases[node.alias.lower()] = canonical
    selected_tables = _unique(selected_tables)

    linked_columns: list[str] = []
    node_links: dict[int, str] = {}
    for node in tree.find_all(exp.Column):
        owner = aliases.get(node.table.lower(), "") if node.table else ""
        if not owner and len(selected_tables) == 1:
            owner = selected_tables[0]
        linked = _resolve_column(owner, node.name, tables) if owner else ""
        if not linked and not node.table:
            matches = [
                _resolve_column(table_name, node.name, tables)
                for table_name in selected_tables
            ]
            matches = [match for match in matches if match]
            linked = matches[0] if len(matches) == 1 else ""
        if linked:
            linked_columns.append(linked)
            node_links[id(node)] = linked

    projections: list[str] = []
    for expression in tree.expressions:
        if isinstance(expression, exp.Column) and node_links.get(id(expression)):
            projections.append(node_links[id(expression)])
        for column in expression.find_all(exp.Column):
            if node_links.get(id(column)):
                projections.append(node_links[id(column)])

    explicit_pairs = {
        frozenset((item["left"], item["right"]))
        for item in _explicit_joins(question, tables)
    }
    joins: list[Mapping[str, str]] = []
    for equality in tree.find_all(exp.EQ):
        if not isinstance(equality.left, exp.Column) or not isinstance(equality.right, exp.Column):
            continue
        left = node_links.get(id(equality.left), "")
        right = node_links.get(id(equality.right), "")
        if not left or not right or left.split(".", 1)[0] == right.split(".", 1)[0]:
            continue
        ancestor = equality.find_ancestor(exp.Join)
        joins.append(
            {
                "left": left,
                "right": right,
                "type": (
                    "left"
                    if ancestor and str(ancestor.args.get("side") or "").upper() == "LEFT"
                    else "inner"
                ),
                "source": (
                    "user_explicit"
                    if frozenset((left, right)) in explicit_pairs
                    else "draft_inferred"
                ),
                "evidence_id": "",
            }
        )
    return {
        "valid": bool(selected_tables),
        "tables": selected_tables,
        "columns": _unique(linked_columns),
        "projection_columns": _unique(projections),
        "joins": joins,
        "error": "" if selected_tables else "no_authorized_table",
    }


def _render_ddl(table: Mapping[str, Any]) -> str:
    primary = set(str(value) for value in table.get("primary_key") or ())
    definitions = []
    for column in table.get("columns") or ():
        name = str(column.get("name") or "")
        if not name:
            continue
        parts = [
            '"%s"' % name.replace('"', '""'),
            str(column.get("column_type") or column.get("data_type") or "TEXT"),
        ]
        if not column.get("nullable", True):
            parts.append("NOT NULL")
        if name in primary:
            parts.append("PRIMARY KEY")
        comment = str(column.get("comment") or "").strip()
        if comment:
            parts.append("/* %s */" % comment.replace("*/", ""))
        definitions.append("  " + " ".join(parts))
    return 'CREATE TABLE "%s" (\n%s\n);' % (
        str(table["name"]).replace('"', '""'),
        ",\n".join(definitions),
    )


def _question_links(
    question: str,
    snapshot: Mapping[str, Any],
) -> Mapping[str, Any]:
    tables = _table_map(snapshot)
    linked_tables: list[str] = []
    linked_columns: list[str] = []
    values: list[Mapping[str, str]] = []
    lower_question = question.lower()
    quoted_values = set(_QUOTED_VALUE.findall(question))

    for token in _TABLE_TOKEN.findall(question):
        table_name = _canonical(token, tuple(tables))
        if table_name:
            linked_tables.append(table_name)
    for table_token, column_token in _QUALIFIED_TOKEN.findall(question):
        linked = _resolve_column(table_token, column_token, tables)
        if linked:
            linked_tables.append(linked.split(".", 1)[0])
            linked_columns.append(linked)

    # A table is commonly named once and its physical columns are then bare.
    candidate_tables = _unique(linked_tables) or list(tables)
    for table_name in candidate_tables:
        for column_name, column in _column_map(tables[table_name]).items():
            comment = str(column.get("comment") or "").strip()
            if column_name.lower() in lower_question or (
                len(comment) >= 2 and comment in question
            ):
                linked_columns.append("%s.%s" % (table_name, column_name))
                linked_tables.append(table_name)
            profile_values = (column.get("profile") or {}).get(
                "low_cardinality_values"
            ) or ()
            for raw_value in profile_values:
                value = str(raw_value)
                if value in quoted_values:
                    linked = "%s.%s" % (table_name, column_name)
                    linked_columns.append(linked)
                    linked_tables.append(table_name)
                    values.append(
                        {"column": linked, "value": value, "source": "profile_exact"}
                    )

    joins = _explicit_joins(question, tables)
    for item in joins:
        linked_tables.extend(
            (item["left"].split(".", 1)[0], item["right"].split(".", 1)[0])
        )
        linked_columns.extend((item["left"], item["right"]))
    return {
        "tables": _unique(linked_tables),
        "columns": _unique(linked_columns),
        "values": values,
        "joins": joins,
    }


def build_draft_link_pack(
    question: str,
    snapshot: Mapping[str, Any],
    *,
    draft_sql: str = "",
    evidence: Sequence[Mapping[str, Any]] = (),
    draft_error: str = "",
    max_tables: int = 6,
) -> Mapping[str, Any]:
    """Merge direct linking and draft reverse-linking into a Grounding input."""

    parsed = parse_draft_sql(draft_sql, question, snapshot)
    direct = _question_links(question, snapshot)
    table_names = _unique([*direct["tables"], *parsed["tables"]])[:max_tables]
    columns = _unique([*direct["columns"], *parsed["columns"]])
    columns = [value for value in columns if value.split(".", 1)[0] in table_names]
    joins: list[Mapping[str, str]] = []
    seen_joins: set[frozenset[str]] = set()
    for item in [*direct["joins"], *parsed["joins"]]:
        key = frozenset((str(item["left"]), str(item["right"])))
        if key in seen_joins:
            continue
        seen_joins.add(key)
        joins.append(dict(item))

    tables = _table_map(snapshot)
    evidence_ids = _unique(
        [
            str(item.get("evidence_id") or "")
            for item in evidence
            if isinstance(item, Mapping)
        ]
    )
    links = []
    for value in columns:
        sources = []
        if value in direct["columns"]:
            sources.append("question_direct")
        if value in parsed["columns"]:
            sources.append("draft_ast")
        links.append({"identifier": value, "sources": sources})
    return {
        "contract": "DraftLinkPack/v1",
        "trust": "untrusted_candidate_input_to_grounding",
        "draft_sql": str(draft_sql or ""),
        "draft_valid": bool(parsed["valid"]),
        "draft_error": str(draft_error or parsed["error"]),
        "tables": table_names,
        "columns": columns,
        "projection_columns": [
            value for value in parsed["projection_columns"] if value in columns
        ],
        "joins": joins,
        "value_links": list(direct["values"]),
        "links": links,
        "full_ddl": [
            {"table": table_name, "ddl": _render_ddl(tables[table_name])}
            for table_name in table_names
        ],
        "retrieval_evidence_ids": evidence_ids,
        "coverage": {
            "has_table": bool(table_names),
            "has_column": bool(columns),
            "has_full_ddl": bool(table_names),
            "has_join": bool(joins),
            "needs_grounding_decision": True,
        },
    }
