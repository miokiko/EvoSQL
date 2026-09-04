"""Build deterministic Text2SQL schema snapshots from a MySQL dump.

The dump path is the cold-start path and needs no running database. A later
live snapshot can reuse the same artifact contract after the isolated MySQL
instance has imported the dump.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Optional, Sequence


_CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+`(?P<name>[^`]+)`\s*\((?P<body>.*?)\)\s*ENGINE\s*=\s*(?P<engine>\w+)(?P<options>.*?);",
    re.IGNORECASE | re.DOTALL,
)
_INSERT = re.compile(
    r"^INSERT\s+INTO\s+`(?P<table>[^`]+)`\s+VALUES\s*\((?P<values>.*)\);\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_SQL_STRING = r"'(?:\\.|''|[^'])*'"
_NUMERIC_TYPES = {
    "bigint",
    "decimal",
    "double",
    "float",
    "int",
    "integer",
    "mediumint",
    "numeric",
    "real",
    "smallint",
    "tinyint",
}


@dataclass(frozen=True)
class SnapshotArtifacts:
    snapshot: Mapping[str, Any]
    join_candidates: Sequence[Mapping[str, Any]]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decode_sql_string(token: str) -> str:
    value = token[1:-1]
    output: list[str] = []
    index = 0
    escapes = {
        "0": "\0",
        "b": "\b",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "Z": "\x1a",
    }
    while index < len(value):
        char = value[index]
        if char == "'" and index + 1 < len(value) and value[index + 1] == "'":
            output.append("'")
            index += 2
            continue
        if char == "\\" and index + 1 < len(value):
            index += 1
            output.append(escapes.get(value[index], value[index]))
            index += 1
            continue
        output.append(char)
        index += 1
    return "".join(output)


def _extract_string_option(text: str, name: str) -> Optional[str]:
    match = re.search(r"\b%s\s+(%s)" % (re.escape(name), _SQL_STRING), text, re.IGNORECASE)
    return _decode_sql_string(match.group(1)) if match else None


def _split_values(text: str) -> list[str]:
    values: list[str] = []
    start = 0
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "'":
                if index + 1 < len(text) and text[index + 1] == "'":
                    index += 1
                else:
                    in_string = False
        elif char == "'":
            in_string = True
        elif char == ",":
            values.append(text[start:index].strip())
            start = index + 1
        index += 1
    values.append(text[start:].strip())
    return values


def _decode_value(token: str) -> Any:
    if token.upper() == "NULL":
        return None
    if len(token) >= 2 and token[0] == token[-1] == "'":
        return _decode_sql_string(token)
    return token


def _parse_column(line: str, ordinal: int) -> Optional[dict[str, Any]]:
    match = re.match(r"`(?P<name>(?:``|[^`])+)`\s+(?P<definition>.*?)(?:,\s*)?$", line.strip())
    if not match:
        return None
    definition = match.group("definition")
    boundary = re.search(
        r"\s+(?=(?:CHARACTER\s+SET|COLLATE|NOT\s+NULL|NULL\b|DEFAULT\b|COMMENT\b|AUTO_INCREMENT\b|ON\s+UPDATE\b|GENERATED\b))",
        definition,
        re.IGNORECASE,
    )
    column_type = definition[: boundary.start()].strip() if boundary else definition.strip()
    type_match = re.match(r"([A-Za-z]+)", column_type)
    data_type = type_match.group(1).lower() if type_match else column_type.lower()
    default_match = re.search(
        r"\bDEFAULT\s+(?P<value>%s|[^\s,]+)" % _SQL_STRING,
        definition,
        re.IGNORECASE,
    )
    default: Any = None
    if default_match:
        token = default_match.group("value")
        default = _decode_value(token)
    return {
        "name": match.group("name").replace("``", "`"),
        "ordinal": ordinal,
        "data_type": data_type,
        "column_type": column_type,
        "nullable": not bool(re.search(r"\bNOT\s+NULL\b", definition, re.IGNORECASE)),
        "default": default,
        "auto_increment": bool(re.search(r"\bAUTO_INCREMENT\b", definition, re.IGNORECASE)),
        "comment": _extract_string_option(definition, "COMMENT") or "",
    }


def _key_columns(fragment: str) -> list[str]:
    return [name.replace("``", "`") for name in re.findall(r"`((?:``|[^`])+)`", fragment)]


def _parse_table(match: re.Match[str]) -> dict[str, Any]:
    body = match.group("body")
    columns: list[dict[str, Any]] = []
    primary_key: list[str] = []
    indexes: list[dict[str, Any]] = []
    foreign_keys: list[dict[str, Any]] = []
    for raw_line in body.splitlines():
        line = raw_line.strip().rstrip(",")
        column = _parse_column(line, len(columns) + 1)
        if column:
            columns.append(column)
            continue
        primary = re.search(r"PRIMARY\s+KEY\s*\((.*?)\)", line, re.IGNORECASE)
        if primary:
            primary_key = _key_columns(primary.group(1))
            continue
        index = re.search(
            r"(?P<unique>UNIQUE\s+)?(?:KEY|INDEX)\s+`(?P<name>[^`]+)`\s*\((?P<columns>.*?)\)",
            line,
            re.IGNORECASE,
        )
        if index:
            indexes.append(
                {
                    "name": index.group("name"),
                    "unique": bool(index.group("unique")),
                    "columns": _key_columns(index.group("columns")),
                }
            )
        foreign = re.search(
            r"(?:CONSTRAINT\s+`(?P<name>[^`]+)`\s+)?FOREIGN\s+KEY\s*\((?P<columns>.*?)\)\s+REFERENCES\s+`(?P<table>[^`]+)`\s*\((?P<referenced>.*?)\)",
            line,
            re.IGNORECASE,
        )
        if foreign:
            foreign_keys.append(
                {
                    "name": foreign.group("name") or "",
                    "columns": _key_columns(foreign.group("columns")),
                    "referenced_table": foreign.group("table"),
                    "referenced_columns": _key_columns(foreign.group("referenced")),
                }
            )
    options = match.group("options")
    collation = re.search(r"\bCOLLATE\s*=\s*([^\s]+)", options, re.IGNORECASE)
    return {
        "name": match.group("name"),
        "engine": match.group("engine"),
        "collation": collation.group(1) if collation else "",
        "comment": _extract_string_option(options, "COMMENT") or "",
        "primary_key": primary_key,
        "indexes": sorted(indexes, key=lambda item: item["name"]),
        "foreign_keys": sorted(foreign_keys, key=lambda item: item["name"]),
        "columns": columns,
    }


def _profile_rows(sql: str, tables: Sequence[MutableMapping[str, Any]]) -> int:
    by_name = {table["name"]: table for table in tables}
    accumulators: dict[str, list[dict[str, Any]]] = {}
    for table in tables:
        accumulators[table["name"]] = [
            {"null_count": 0, "values": set(), "min": None, "max": None, "max_length": 0}
            for _ in table["columns"]
        ]
        table["row_count"] = 0

    parsed_rows = 0
    for match in _INSERT.finditer(sql):
        table = by_name.get(match.group("table"))
        if table is None:
            continue
        raw_values = _split_values(match.group("values"))
        if len(raw_values) != len(table["columns"]):
            raise ValueError(
                "INSERT column count mismatch for %s: expected %d, got %d"
                % (table["name"], len(table["columns"]), len(raw_values))
            )
        table["row_count"] += 1
        parsed_rows += 1
        for index, (column, token) in enumerate(zip(table["columns"], raw_values)):
            value = _decode_value(token)
            stats = accumulators[table["name"]][index]
            if value is None:
                stats["null_count"] += 1
                continue
            marker = str(value)
            stats["values"].add(marker)
            stats["max_length"] = max(stats["max_length"], len(marker))
            if column["data_type"] in _NUMERIC_TYPES:
                try:
                    number = Decimal(marker)
                except InvalidOperation:
                    continue
                stats["min"] = number if stats["min"] is None else min(stats["min"], number)
                stats["max"] = number if stats["max"] is None else max(stats["max"], number)

    for table in tables:
        row_count = table["row_count"]
        for column, stats in zip(table["columns"], accumulators[table["name"]]):
            values = sorted(stats["values"])
            profile: dict[str, Any] = {
                "null_count": stats["null_count"],
                "null_ratio": round(stats["null_count"] / row_count, 6) if row_count else 0.0,
                "distinct_count": len(values),
                "max_length": stats["max_length"],
            }
            if len(values) <= 20 and stats["max_length"] <= 128:
                profile["low_cardinality_values"] = values
            if stats["min"] is not None:
                profile["min"] = str(stats["min"])
                profile["max"] = str(stats["max"])
            column["profile"] = profile
            column["_distinct_values"] = values
    return parsed_rows


def _normalized_key(name: str) -> str:
    value = re.sub(r"^(?:c|i|d|f|n|s)_", "", name.strip(), flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value.lower())


def infer_join_candidates(tables: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    table_lookup = {table["name"]: table for table in tables}
    column_lookup = {
        "%s.%s" % (table["name"], column["name"]): column
        for table in tables
        for column in table["columns"]
    }

    def enrich(candidate: dict[str, Any]) -> dict[str, Any]:
        left_column = column_lookup[candidate["left"]]
        right_column = column_lookup[candidate["right"]]
        left_values = set(left_column.get("_distinct_values", []))
        right_values = set(right_column.get("_distinct_values", []))
        if left_values and right_values:
            intersection = left_values & right_values
            union = left_values | right_values
            candidate["data_overlap"] = {
                "intersection_count": len(intersection),
                "left_coverage": round(len(intersection) / len(left_values), 6),
                "right_coverage": round(len(intersection) / len(right_values), 6),
                "jaccard": round(len(intersection) / len(union), 6),
            }
            if not intersection:
                candidate["basis"].append("no_observed_value_overlap")
                candidate["confidence"] = min(candidate["confidence"], 0.25)
            elif min(
                candidate["data_overlap"]["left_coverage"],
                candidate["data_overlap"]["right_coverage"],
            ) >= 0.8:
                candidate["basis"].append("high_observed_value_overlap")
                candidate["confidence"] = min(0.98, candidate["confidence"] + 0.05)

        left_table_name, left_name = candidate["left"].split(".", 1)
        right_table_name, right_name = candidate["right"].split(".", 1)

        def unique(table_name: str, column_name: str, column: Mapping[str, Any]) -> bool:
            table = table_lookup[table_name]
            if table.get("primary_key") == [column_name]:
                return True
            non_null = table.get("row_count", 0) - column.get("profile", {}).get("null_count", 0)
            return bool(non_null) and column.get("profile", {}).get("distinct_count") == non_null

        left_unique = unique(left_table_name, left_name, left_column)
        right_unique = unique(right_table_name, right_name, right_column)
        if left_unique and right_unique:
            candidate["cardinality"] = "one_to_one_candidate"
        elif left_unique or right_unique:
            candidate["cardinality"] = "one_to_many_candidate"
        else:
            candidate["cardinality"] = "many_to_many_or_unknown"
        return candidate

    candidates: dict[str, dict[str, Any]] = {}
    for table in tables:
        for foreign in table.get("foreign_keys", []):
            for left_column, right_column in zip(
                foreign["columns"], foreign["referenced_columns"]
            ):
                endpoints = sorted(
                    [
                        "%s.%s" % (table["name"], left_column),
                        "%s.%s" % (foreign["referenced_table"], right_column),
                    ]
                )
                candidate_id = "join_" + _fingerprint(endpoints)[:16]
                candidates[candidate_id] = {
                    "candidate_id": candidate_id,
                    "left": endpoints[0],
                    "right": endpoints[1],
                    "basis": ["declared_foreign_key"],
                    "confidence": 1.0,
                    "cardinality": "unknown",
                    "data_overlap": None,
                }

    for left_index, left_table in enumerate(tables):
        for right_table in tables[left_index + 1 :]:
            for left_column in left_table["columns"]:
                for right_column in right_table["columns"]:
                    left_key = _normalized_key(left_column["name"])
                    right_key = _normalized_key(right_column["name"])
                    if not left_key or left_key != right_key:
                        continue
                    looks_like_key = bool(re.search(r"(?:id|code|no)$", left_key, re.IGNORECASE))
                    left_primary = left_column["name"] in left_table["primary_key"]
                    right_primary = right_column["name"] in right_table["primary_key"]
                    if not (looks_like_key or left_primary or right_primary):
                        continue
                    endpoints = sorted(
                        [
                            "%s.%s" % (left_table["name"], left_column["name"]),
                            "%s.%s" % (right_table["name"], right_column["name"]),
                        ]
                    )
                    candidate_id = "join_" + _fingerprint(endpoints)[:16]
                    basis = ["matching_normalized_column_name"]
                    if left_column["name"].lower() == right_column["name"].lower():
                        basis.append("matching_exact_column_name")
                    if left_primary:
                        basis.append("left_primary_key")
                    if right_primary:
                        basis.append("right_primary_key")
                    confidence = 0.65
                    if "matching_exact_column_name" in basis:
                        confidence = 0.75
                    if left_primary or right_primary:
                        confidence = 0.9
                    candidates.setdefault(
                        candidate_id,
                        {
                            "candidate_id": candidate_id,
                            "left": endpoints[0],
                            "right": endpoints[1],
                            "basis": basis,
                            "confidence": confidence,
                            "cardinality": "unknown",
                            "data_overlap": None,
                        },
                    )
    enriched = [enrich(candidate) for candidate in candidates.values()]
    return sorted(enriched, key=lambda item: (item["left"], item["right"]))


def build_snapshot_from_dump(path: Path, database_name: str = "evo_text2sql_eval") -> SnapshotArtifacts:
    dump_path = path.resolve()
    sql = dump_path.read_text(encoding="utf-8-sig")
    tables = [_parse_table(match) for match in _CREATE_TABLE.finditer(sql)]
    if not tables:
        raise ValueError("no CREATE TABLE statements found in %s" % dump_path)
    tables.sort(key=lambda item: item["name"])
    parsed_rows = _profile_rows(sql, tables)
    schema_contract = [
        {
            "name": table["name"],
            "engine": table["engine"],
            "collation": table["collation"],
            "comment": table["comment"],
            "primary_key": table["primary_key"],
            "indexes": table["indexes"],
            "foreign_keys": table["foreign_keys"],
            "columns": [
                {key: value for key, value in column.items() if key not in {"profile", "_distinct_values"}}
                for column in table["columns"]
            ],
        }
        for table in tables
    ]
    schema_fingerprint = _fingerprint(schema_contract)
    profile_fingerprint = _fingerprint(
        [
            {
                "name": table["name"],
                "row_count": table["row_count"],
                "profiles": [column["profile"] for column in table["columns"]],
            }
            for table in tables
        ]
    )
    dump_sha256 = sha256_file(dump_path)
    join_candidates = infer_join_candidates(tables)
    snapshot_id = "dbs_" + _fingerprint(
        {
            "database": database_name,
            "dump_sha256": dump_sha256,
            "schema_fingerprint": schema_fingerprint,
            "profile_fingerprint": profile_fingerprint,
        }
    )[:20]
    for table in tables:
        for column in table["columns"]:
            column.pop("_distinct_values", None)

    snapshot = {
        "contract_version": 1,
        "snapshot_id": snapshot_id,
        "database": database_name,
        "source": {
            "kind": "mysql_dump",
            "path": str(dump_path),
            "dump_sha256": dump_sha256,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schema_fingerprint": schema_fingerprint,
        "profile_fingerprint": profile_fingerprint,
        "table_count": len(tables),
        "row_count": parsed_rows,
        "tables": tables,
    }
    return SnapshotArtifacts(snapshot=snapshot, join_candidates=join_candidates)


def _merge_reviews(
    candidates: Sequence[Mapping[str, Any]], existing: Optional[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    old = {
        item["candidate_id"]: item
        for item in (existing or {}).get("relationships", [])
        if "candidate_id" in item
    }
    relationships: list[dict[str, Any]] = []
    for candidate in candidates:
        previous = old.get(candidate["candidate_id"], {})
        relationships.append(
            {
                **candidate,
                "decision": previous.get("decision", "pending"),
                "cardinality": previous.get("cardinality", candidate["cardinality"]),
                "result_grain": previous.get("result_grain", ""),
                "fanout_risk": previous.get("fanout_risk", "unknown"),
                "reviewer": previous.get("reviewer", ""),
                "reviewed_at": previous.get("reviewed_at", ""),
                "notes": previous.get("notes", ""),
            }
        )
    return relationships


def write_snapshot_artifacts(artifacts: SnapshotArtifacts, output_dir: Path) -> Mapping[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = output_dir / "database_snapshot.json"
    candidates_path = output_dir / "join_candidates.json"
    review_path = output_dir / "join_catalog.review.json"
    existing: Optional[Mapping[str, Any]] = None
    if review_path.exists():
        existing = json.loads(review_path.read_text(encoding="utf-8"))
    snapshot_id = artifacts.snapshot["snapshot_id"]
    if existing and existing.get("database_snapshot_id") != snapshot_id:
        existing = None
    snapshot_path.write_text(
        json.dumps(artifacts.snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    candidates_document = {
        "contract_version": 1,
        "database_snapshot_id": snapshot_id,
        "status": "unreviewed",
        "relationships": list(artifacts.join_candidates),
    }
    candidates_path.write_text(
        json.dumps(candidates_document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    relationships = _merge_reviews(artifacts.join_candidates, existing)
    review_document = {
        "contract_version": 1,
        "database_snapshot_id": snapshot_id,
        "status": "approved" if relationships and all(item["decision"] != "pending" for item in relationships) else "pending_review",
        "instructions": "Set decision to approved or rejected; fill cardinality, result_grain, fanout_risk, reviewer and notes.",
        "relationships": relationships,
    }
    review_path.write_text(
        json.dumps(review_document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest = {
        "contract_version": 1,
        "database_snapshot_id": snapshot_id,
        "files": {
            "database_snapshot.json": sha256_file(snapshot_path),
            "join_candidates.json": sha256_file(candidates_path),
            "join_catalog.review.json": sha256_file(review_path),
        },
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "snapshot": snapshot_path,
        "join_candidates": candidates_path,
        "join_review": review_path,
        "manifest": manifest_path,
    }
