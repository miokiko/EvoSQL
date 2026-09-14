"""Deterministic schema expansion around an untrusted draft SQL proposal.

The draft is never executed. It is parsed only to recover schema candidates,
which are checked against the pinned snapshot and handed to Grounding.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
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


def _identifier_is_explicit(question: str, identifier: str) -> bool:
    """Match an ASCII identifier without authorizing a larger token substring."""

    return bool(
        re.search(
            r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(identifier),
            question,
            re.IGNORECASE,
        )
    )


def _profile_physical_value(value: str, data_type: str) -> Any:
    """Convert snapshot profile text to the SQLite affinity used by the mirror."""

    integer_types = {"bigint", "int", "integer", "mediumint", "smallint", "tinyint"}
    real_types = {"decimal", "double", "float", "numeric", "real"}
    kind = str(data_type or "").casefold()
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return value
    if not number.is_finite():
        return value
    if kind in integer_types and number == number.to_integral_value():
        return int(number)
    if kind in real_types:
        return float(number)
    return value


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


def _column_owner_index(
    tables: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, list[str]]:
    """Index every physical column name to all of its snapshot-owned tables."""

    owners: dict[str, list[str]] = {}
    for table_name, table in tables.items():
        for column_name in _column_map(table):
            owners.setdefault(column_name.casefold(), []).append(
                "%s.%s" % (table_name, column_name)
            )
    return owners


def _column_contexts(node: exp.Column) -> tuple[str, ...]:
    """Return the SQL clauses in which one physical-column reference appears."""

    contexts = []
    if node.find_ancestor(exp.Where) is not None or node.find_ancestor(exp.Having) is not None:
        contexts.append("filter")
    if node.find_ancestor(exp.Group) is not None:
        contexts.append("group")
    if node.find_ancestor(exp.Order) is not None:
        contexts.append("order")
    if node.find_ancestor(exp.Join) is not None:
        contexts.append("join")
    return tuple(contexts)


def _is_projection_column(node: exp.Column) -> bool:
    """Return whether ``node`` is below a SELECT-list expression."""

    select = node.find_ancestor(exp.Select)
    if select is None:
        return False
    child: exp.Expression = node
    while child.parent is not None and child.parent is not select:
        child = child.parent
    return child in select.expressions


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
    """Parse an untrusted SELECT and return snapshot-checked schema candidates.

    Unknown and ambiguous identifiers are retained only as diagnostic/completion
    hints.  They never enter ``tables`` or ``columns``, whose values are always
    resolved against the pinned snapshot.
    """

    empty = {
        "valid": False,
        "tables": [],
        "columns": [],
        "projection_columns": [],
        "filter_columns": [],
        "group_columns": [],
        "order_columns": [],
        "join_columns": [],
        "unresolved_columns": [],
        "ambiguous_columns": [],
        "column_owners": {},
        "has_star": False,
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

    owner_index = _column_owner_index(tables)
    linked_columns: list[str] = []
    node_links: dict[int, str] = {}
    unresolved_columns: list[str] = []
    ambiguous_columns: list[Mapping[str, Any]] = []
    observed_names: list[str] = []
    for node in tree.find_all(exp.Column):
        observed_names.append(node.name)
        owner = aliases.get(node.table.lower(), "") if node.table else ""
        if not owner and len(selected_tables) == 1:
            owner = selected_tables[0]
        linked = _resolve_column(owner, node.name, tables) if owner else ""
        matches: list[str] = []
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
            continue

        raw_identifier = (
            "%s.%s" % (node.table, node.name) if node.table else node.name
        )
        if len(matches) > 1:
            ambiguous_columns.append(
                {
                    "identifier": raw_identifier,
                    "candidates": _unique(matches),
                }
            )
        else:
            unresolved_columns.append(raw_identifier)

    projections: list[str] = []
    clause_columns: dict[str, list[str]] = {
        "filter": [],
        "group": [],
        "order": [],
        "join": [],
    }
    for node in tree.find_all(exp.Column):
        linked = node_links.get(id(node), "")
        if not linked:
            continue
        if _is_projection_column(node):
            projections.append(linked)
        for context in _column_contexts(node):
            clause_columns[context].append(linked)

    # Preserve one diagnostic row per ambiguous spelling while merging the
    # candidate owners found in separate clauses/subqueries.
    ambiguous_by_identifier: dict[str, list[str]] = {}
    for item in ambiguous_columns:
        identifier = str(item["identifier"])
        ambiguous_by_identifier.setdefault(identifier, []).extend(
            str(value) for value in item.get("candidates") or ()
        )
    normalized_ambiguous = [
        {"identifier": identifier, "candidates": _unique(candidates)}
        for identifier, candidates in ambiguous_by_identifier.items()
    ]

    column_owners = {
        name: list(owner_index.get(name.casefold(), ()))
        for name in _unique(observed_names)
        if owner_index.get(name.casefold())
    }

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
        "filter_columns": _unique(clause_columns["filter"]),
        "group_columns": _unique(clause_columns["group"]),
        "order_columns": _unique(clause_columns["order"]),
        "join_columns": _unique(clause_columns["join"]),
        "unresolved_columns": _unique(unresolved_columns),
        "ambiguous_columns": normalized_ambiguous,
        "column_owners": column_owners,
        "has_star": any(True for _ in tree.find_all(exp.Star)),
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
    values: list[Mapping[str, Any]] = []
    column_aliases: dict[str, list[str]] = {}
    column_sources: dict[str, list[str]] = {}
    quoted_values = set(_QUOTED_VALUE.findall(question))

    def record_column(linked: str, alias: str, source: str) -> None:
        if not linked:
            return
        linked_columns.append(linked)
        if alias:
            column_aliases.setdefault(linked, [])
            if alias not in column_aliases[linked]:
                column_aliases[linked].append(alias)
        column_sources.setdefault(linked, [])
        if source and source not in column_sources[linked]:
            column_sources[linked].append(source)

    for token in _TABLE_TOKEN.findall(question):
        table_name = _canonical(token, tuple(tables))
        if table_name:
            linked_tables.append(table_name)
    for table_token, column_token in _QUALIFIED_TOKEN.findall(question):
        linked = _resolve_column(table_token, column_token, tables)
        if linked:
            linked_tables.append(linked.split(".", 1)[0])
            record_column(linked, linked.split(".", 1)[1], "question_identifier")

    # A table is commonly named once and its physical columns are then bare.
    candidate_tables = _unique(linked_tables) or list(tables)
    for table_name in candidate_tables:
        for column_name, column in _column_map(tables[table_name]).items():
            comment = str(column.get("comment") or "").strip()
            identifier_explicit = _identifier_is_explicit(question, column_name)
            comment_explicit = len(comment) >= 2 and comment in question
            if identifier_explicit or comment_explicit:
                linked = "%s.%s" % (table_name, column_name)
                linked_tables.append(table_name)
                # Prefer the user's business wording over a physical identifier
                # when both surface forms are present.
                if comment_explicit:
                    record_column(linked, comment, "snapshot_comment_exact")
                    aliases = column_aliases.get(linked, [])
                    column_aliases[linked] = [
                        comment,
                        *(item for item in aliases if item != comment),
                    ]
                if identifier_explicit:
                    record_column(linked, column_name, "question_identifier")
            profile_values = (column.get("profile") or {}).get(
                "low_cardinality_values"
            ) or ()
            for raw_value in profile_values:
                value = str(raw_value)
                if value in quoted_values:
                    linked = "%s.%s" % (table_name, column_name)
                    record_column(
                        linked,
                        comment if comment_explicit else (
                            column_name if identifier_explicit else ""
                        ),
                        "profile_value_exact",
                    )
                    linked_tables.append(table_name)
                    values.append(
                        {
                            "column": linked,
                            "value": value,
                            "logical_value": value,
                            "physical_value": _profile_physical_value(
                                value, str(column.get("data_type") or "")
                            ),
                            "source": "profile_exact",
                        }
                    )

    joins = _explicit_joins(question, tables)
    for item in joins:
        linked_tables.extend(
            (item["left"].split(".", 1)[0], item["right"].split(".", 1)[0])
        )
        for endpoint in (item["left"], item["right"]):
            record_column(
                endpoint,
                endpoint.split(".", 1)[1],
                "question_identifier",
            )
    return {
        "tables": _unique(linked_tables),
        "columns": _unique(linked_columns),
        "values": values,
        "joins": joins,
        "column_aliases": {
            column: aliases for column, aliases in column_aliases.items()
        },
        "column_sources": {
            column: sources for column, sources in column_sources.items()
        },
    }


def _candidate_items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, Mapping)):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, bytes):
        return list(value)
    return []


def _forward_source(value: Any, default: str = "llm_forward") -> str:
    normalized = str(value or default).strip().casefold().replace("-", "_")
    if normalized in {"keyword", "keyword_match", "lexical", "lexical_match"}:
        return "keyword_match"
    return "llm_forward"


def _forward_candidate_links(
    value: Mapping[str, Any],
    snapshot: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Normalize untrusted forward links against the pinned snapshot."""

    tables = _table_map(snapshot)
    owners = _column_owner_index(tables)
    default_source = _forward_source(value.get("source"))
    linked_tables: list[str] = []
    linked_columns: list[str] = []
    joins: list[Mapping[str, str]] = []
    logical_concepts: list[Mapping[str, Any]] = []
    column_sources: dict[str, list[str]] = {}
    column_aliases: dict[str, list[str]] = {}

    def record_column(identifier: str, source: str, aliases: Sequence[str] = ()) -> None:
        linked_columns.append(identifier)
        linked_tables.append(identifier.split(".", 1)[0])
        column_sources.setdefault(identifier, [])
        if source not in column_sources[identifier]:
            column_sources[identifier].append(source)
        column_aliases.setdefault(identifier, [])
        for alias in aliases:
            rendered = str(alias).strip()
            if rendered and rendered not in column_aliases[identifier]:
                column_aliases[identifier].append(rendered)

    def resolve_candidate(identifier: str) -> list[str]:
        rendered = str(identifier or "").strip()
        if not rendered:
            return []
        if "." in rendered:
            table_name, column_name = rendered.split(".", 1)
            linked = _resolve_column(table_name, column_name, tables)
            return [linked] if linked else []
        return list(owners.get(rendered.casefold(), ()))

    for raw in _candidate_items(value.get("tables")):
        rendered = str(
            raw.get("table") or raw.get("name") or raw.get("identifier") or ""
            if isinstance(raw, Mapping)
            else raw
        ).strip()
        table_name = _canonical(rendered, tuple(tables))
        if table_name:
            linked_tables.append(table_name)

    for raw in _candidate_items(value.get("columns")):
        if isinstance(raw, Mapping):
            identifier = str(
                raw.get("column") or raw.get("identifier") or raw.get("name") or ""
            )
            source = _forward_source(raw.get("source"), default_source)
            aliases = [
                str(item)
                for item in (
                    raw.get("logical_name"),
                    raw.get("alias"),
                    *(raw.get("aliases") or ()),
                )
                if str(item or "").strip()
            ]
        else:
            identifier = str(raw)
            source = default_source
            aliases = []
        resolved = resolve_candidate(identifier)
        for column in resolved:
            record_column(column, source, aliases)
            if "." not in identifier:
                if "snapshot_expansion" not in column_sources[column]:
                    column_sources[column].append("snapshot_expansion")
        if aliases:
            logical_name = aliases[0]
            for column in resolved:
                logical_concepts.append(
                    {
                        "logical_name": logical_name,
                        "aliases": aliases[1:],
                        "column": column,
                        "sources": list(column_sources[column]),
                    }
                )

    for raw in _candidate_items(value.get("logical_concepts")):
        if not isinstance(raw, Mapping):
            continue
        source = _forward_source(raw.get("source"), default_source)
        resolved = resolve_candidate(
            str(raw.get("column") or raw.get("identifier") or "")
        )
        logical_name = str(
            raw.get("logical_name") or raw.get("concept") or raw.get("name") or ""
        ).strip()
        aliases = [
            str(item).strip()
            for item in raw.get("aliases") or ()
            if str(item).strip()
        ]
        if not logical_name:
            continue
        for column in resolved:
            record_column(column, source, (logical_name, *aliases))
            logical_concepts.append(
                {
                    "slot_id": str(raw.get("slot_id") or ""),
                    "logical_name": logical_name,
                    "aliases": aliases,
                    "column": column,
                    "sources": list(column_sources[column]),
                }
            )

    for raw in _candidate_items(value.get("joins")):
        if not isinstance(raw, Mapping):
            continue
        source = _forward_source(raw.get("candidate_source") or raw.get("source"), default_source)
        left_values = resolve_candidate(str(raw.get("left") or ""))
        right_values = resolve_candidate(str(raw.get("right") or ""))
        if len(left_values) != 1 or len(right_values) != 1:
            continue
        left, right = left_values[0], right_values[0]
        if left.split(".", 1)[0] == right.split(".", 1)[0]:
            continue
        record_column(left, source)
        record_column(right, source)
        joins.append(
            {
                "left": left,
                "right": right,
                "type": (
                    "left" if str(raw.get("type") or "").casefold() == "left" else "inner"
                ),
                # A forward-model or keyword proposal never authorizes a Join.
                "source": "draft_inferred",
                "evidence_id": "",
            }
        )

    return {
        "tables": _unique(linked_tables),
        "columns": _unique(linked_columns),
        "joins": joins,
        "logical_concepts": logical_concepts,
        "column_sources": column_sources,
        "column_aliases": column_aliases,
    }


def build_draft_link_pack(
    question: str,
    snapshot: Mapping[str, Any],
    *,
    draft_sql: str = "",
    evidence: Sequence[Mapping[str, Any]] = (),
    draft_error: str = "",
    max_tables: int = 6,
    forward_candidates: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """Merge forward, keyword and draft-AST links into one Grounding input.

    ``forward_candidates`` is an untrusted optional mapping with ``tables``,
    ``columns``, ``joins`` and ``logical_concepts`` collections. Each collection
    entry may set ``source`` to ``llm_forward`` or ``keyword_match``. Every
    identifier is resolved against ``snapshot`` before it can enter the pack.
    """

    parsed = parse_draft_sql(draft_sql, question, snapshot)
    direct = _question_links(question, snapshot)
    forward = _forward_candidate_links(
        forward_candidates if isinstance(forward_candidates, Mapping) else {},
        snapshot,
    )
    tables = _table_map(snapshot)
    owner_index = _column_owner_index(tables)

    seed_columns = _unique(
        [*direct["columns"], *forward["columns"], *parsed["columns"]]
    )
    expansion_columns: list[str] = []
    # Reverse owner expansion is driven by the draft AST.  A physical column
    # that the user already qualified to one table must not fan out to every
    # same-named column in the database and make a formerly precise request
    # require unsupported joins.
    owner_names = [
        value.split(".", 1)[-1] for value in parsed.get("columns") or ()
    ]
    owner_names.extend(
        str(value).split(".", 1)[-1]
        for value in parsed.get("unresolved_columns") or ()
    )
    for item in parsed.get("ambiguous_columns") or ():
        if not isinstance(item, Mapping):
            continue
        owner_names.append(str(item.get("identifier") or "").split(".", 1)[-1])
        expansion_columns.extend(
            str(value) for value in item.get("candidates") or ()
        )
    for name in _unique(owner_names):
        expansion_columns.extend(owner_index.get(name.casefold(), ()))
    expansion_columns = _unique(expansion_columns)

    table_candidates = _unique(
        [
            *direct["tables"],
            *forward["tables"],
            *parsed["tables"],
            *(value.split(".", 1)[0] for value in expansion_columns),
        ]
    )
    bounded_max_tables = max(1, min(int(max_tables), 50))
    table_names = table_candidates[:bounded_max_tables]
    columns = _unique([*seed_columns, *expansion_columns])
    columns = [value for value in columns if value.split(".", 1)[0] in table_names]
    joins: list[Mapping[str, str]] = []
    seen_joins: set[frozenset[str]] = set()
    for item in [*direct["joins"], *forward["joins"], *parsed["joins"]]:
        if not isinstance(item, Mapping):
            continue
        endpoints = (str(item.get("left") or ""), str(item.get("right") or ""))
        if not all(
            "." in endpoint and endpoint.split(".", 1)[0] in table_names
            for endpoint in endpoints
        ):
            continue
        key = frozenset(endpoints)
        if key in seen_joins:
            continue
        seen_joins.add(key)
        joins.append(dict(item))

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
            sources.extend(direct["column_sources"].get(value, ()))
        sources.extend(forward["column_sources"].get(value, ()))
        if value in parsed["columns"]:
            sources.append("draft_ast")
        if value in expansion_columns and value not in seed_columns:
            sources.append("snapshot_expansion")
        aliases = _unique(
            [
                *direct["column_aliases"].get(value, ()),
                *forward["column_aliases"].get(value, ()),
            ]
        )
        links.append(
            {
                "identifier": value,
                "sources": _unique(sources),
                "aliases": aliases,
            }
        )
    logical_concepts = []
    for index, column in enumerate(direct["columns"]):
        aliases = list(direct["column_aliases"].get(column, ()))
        if not aliases:
            continue
        logical_concepts.append(
            {
                "slot_id": "concept-%d" % (index + 1),
                "logical_name": aliases[0],
                "aliases": aliases[1:],
                "column": column,
                "sources": list(direct["column_sources"].get(column, ())),
            }
        )
    for concept in forward["logical_concepts"]:
        if not isinstance(concept, Mapping):
            continue
        column = str(concept.get("column") or "")
        logical_name = str(concept.get("logical_name") or "").strip()
        if column not in columns or not logical_name:
            continue
        marker = (logical_name.casefold(), column.casefold())
        if any(
            (str(item.get("logical_name") or "").casefold(), str(item.get("column") or "").casefold())
            == marker
            for item in logical_concepts
        ):
            continue
        logical_concepts.append(
            {
                "slot_id": str(concept.get("slot_id") or "concept-%d" % (len(logical_concepts) + 1)),
                "logical_name": logical_name,
                "aliases": _unique(
                    [str(item) for item in concept.get("aliases") or ()]
                ),
                "column": column,
                "sources": _unique(
                    [str(item) for item in concept.get("sources") or ()]
                ),
            }
        )

    all_owner_names = _unique(
        [*owner_names, *(value.split(".", 1)[1] for value in columns)]
    )
    column_owners = {
        name: list(owner_index.get(name.casefold(), ()))
        for name in all_owner_names
        if owner_index.get(name.casefold())
    }
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
        "filter_columns": [
            value for value in parsed["filter_columns"] if value in columns
        ],
        "group_columns": [
            value for value in parsed["group_columns"] if value in columns
        ],
        "order_columns": [
            value for value in parsed["order_columns"] if value in columns
        ],
        "join_columns": [
            value for value in parsed["join_columns"] if value in columns
        ],
        "unresolved_columns": list(parsed["unresolved_columns"]),
        "ambiguous_columns": [dict(item) for item in parsed["ambiguous_columns"]],
        "column_owners": column_owners,
        "has_star": bool(parsed["has_star"]),
        "joins": joins,
        "value_links": list(direct["values"]),
        "links": links,
        "logical_concepts": logical_concepts,
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
            "has_unresolved_column": bool(parsed["unresolved_columns"]),
            "has_ambiguous_column": bool(parsed["ambiguous_columns"]),
            "has_star": bool(parsed["has_star"]),
            "snapshot_expanded": any(
                value not in seed_columns for value in expansion_columns
            ),
            "table_candidates_truncated": len(table_candidates) > len(table_names),
            "needs_grounding_decision": True,
        },
    }
