"""Build an original 240-case benchmark solely from the copied database snapshot."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from .evaluation import result_fingerprint
from .schema_catalog import sha256_file
from .sql_safety import ReadOnlySQLiteExecutor, validate_sql


CATEGORY_TARGETS = {
    "projection_filter": 40,
    "count_group": 40,
    "aggregate_topk": 40,
    "null_existence": 40,
    "join": 50,
    "composite_subquery": 30,
}


def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    return "'%s'" % str(value).replace("'", "''")


def _label(column: Mapping[str, Any]) -> str:
    return str(column.get("comment") or column["name"]).replace("\r", " ").replace("\n", " ")


def _identifier(table: Mapping[str, Any]) -> str:
    if table["primary_key"]:
        return table["primary_key"][0]
    return table["columns"][0]["name"]


def _base_case(
    *,
    question: str,
    sql: str,
    skeleton: str,
    category: str,
    difficulty: str,
    snapshot_id: str,
    tables: Sequence[str],
    columns: Sequence[str],
    relationships: Sequence[str] = (),
    ordered: bool = False,
) -> dict[str, Any]:
    identity = hashlib.sha256(
        json.dumps([question, sql, skeleton], ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:20]
    return {
        "case_id": "t2sql_%s" % identity,
        "question": question,
        "gold_sql": sql,
        "gold_result_fingerprint": "",
        "gold_row_count": 0,
        "gold_column_count": 0,
        "sql_skeleton": skeleton,
        "category": category,
        "difficulty": difficulty,
        "database_snapshot_id": snapshot_id,
        "split": "",
        "ordered": ordered,
        "required_tables": list(dict.fromkeys(tables)),
        "required_columns": list(dict.fromkeys(columns)),
        "required_relationships": list(dict.fromkeys(relationships)),
    }


def _take_unique(cases: Sequence[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    values = []
    seen = set()
    for case in cases:
        key = (case["question"], case["gold_sql"])
        if key in seen:
            continue
        seen.add(key)
        values.append(case)
        if len(values) == count:
            return values
    raise ValueError("not enough unique generated cases: required %d, got %d" % (count, len(values)))


def generate_cases(
    snapshot: Mapping[str, Any], join_catalog: Mapping[str, Any]
) -> list[dict[str, Any]]:
    snapshot_id = snapshot["snapshot_id"]
    tables = [
        table
        for table in snapshot["tables"]
        if table["row_count"] > 0 and table["name"].startswith("t_")
    ]
    value_fields = []
    numeric_fields = []
    nullable_fields = []
    by_table = {table["name"]: table for table in tables}
    for table in tables:
        for column in table["columns"]:
            profile = column.get("profile") or {}
            values = [
                value
                for value in profile.get("low_cardinality_values") or ()
                if value is not None and len(str(value)) <= 48
            ]
            if values and column["data_type"] not in {"blob", "longblob", "mediumblob"}:
                value_fields.append((table, column, values))
            if column["data_type"] in {
                "bigint", "decimal", "double", "float", "int", "integer",
                "mediumint", "numeric", "real", "smallint", "tinyint",
            } and int(profile.get("distinct_count", 0)) > 0:
                numeric_fields.append((table, column))
            if int(profile.get("null_count", 0)) > 0:
                nullable_fields.append((table, column))

    projection = []
    for table, column, values in value_fields:
        output = _identifier(table)
        for value in values[:3]:
            sql = (
                "SELECT DISTINCT {output} FROM {table} WHERE {column} = {value} "
                "ORDER BY {output} LIMIT 50"
            ).format(
                output=output, table=table["name"], column=column["name"], value=_literal(value)
            )
            projection.append(
                _base_case(
                    question=(
                        "在表 {table} 中，列出{label}等于“{value}”的{output}，"
                        "去重后按字段升序排列，最多 50 条。"
                    ).format(
                        table=table["name"], label=_label(column), value=value, output=output
                    ),
                    sql=sql,
                    skeleton="projection_eq:%s:%s:%s" % (table["name"], column["name"], output),
                    category="projection_filter",
                    difficulty="easy",
                    snapshot_id=snapshot_id,
                    tables=(table["name"],),
                    columns=("%s.%s" % (table["name"], column["name"]), "%s.%s" % (table["name"], output)),
                    ordered=True,
                )
            )

    counts = []
    for table, column, values in value_fields:
        identity = _identifier(table)
        for value in values[:2]:
            sql = "SELECT COUNT(DISTINCT {identity}) AS result_count FROM {table} WHERE {column} = {value}".format(
                identity=identity,
                table=table["name"],
                column=column["name"],
                value=_literal(value),
            )
            counts.append(
                _base_case(
                    question="表 {table} 中，{label}等于“{value}”的不同{identity}有多少个？".format(
                        table=table["name"], label=_label(column), value=value, identity=identity
                    ),
                    sql=sql,
                    skeleton="count_distinct_eq:%s:%s:%s" % (table["name"], column["name"], identity),
                    category="count_group",
                    difficulty="easy",
                    snapshot_id=snapshot_id,
                    tables=(table["name"],),
                    columns=("%s.%s" % (table["name"], column["name"]), "%s.%s" % (table["name"], identity)),
                )
            )
        sql = "SELECT {column}, COUNT(*) AS result_count FROM {table} GROUP BY {column} ORDER BY {column}".format(
            column=column["name"], table=table["name"]
        )
        counts.append(
            _base_case(
                question="统计表 {table} 按{label}（{column}）分组的记录数，并按该字段升序排列。".format(
                    table=table["name"], label=_label(column), column=column["name"]
                ),
                sql=sql,
                skeleton="group_count:%s:%s" % (table["name"], column["name"]),
                category="count_group",
                difficulty="medium",
                snapshot_id=snapshot_id,
                tables=(table["name"],),
                columns=("%s.%s" % (table["name"], column["name"]),),
                ordered=True,
            )
        )

    aggregates = []
    for table, column in numeric_fields:
        qualified = "%s.%s" % (table["name"], column["name"])
        for function, word in (("AVG", "平均值"), ("MAX", "最大值"), ("MIN", "最小值")):
            sql = "SELECT ROUND({function}({column}), 6) AS result_value FROM {table} WHERE {column} IS NOT NULL".format(
                function=function, column=column["name"], table=table["name"]
            )
            aggregates.append(
                _base_case(
                    question="表 {table} 的{label}（{column}）非空值的{word}是多少？结果保留六位小数。".format(
                        table=table["name"], label=_label(column), column=column["name"], word=word
                    ),
                    sql=sql,
                    skeleton="aggregate:%s:%s:%s" % (function.lower(), table["name"], column["name"]),
                    category="aggregate_topk",
                    difficulty="medium",
                    snapshot_id=snapshot_id,
                    tables=(table["name"],),
                    columns=(qualified,),
                )
            )
        identity = _identifier(table)
        sql = "SELECT {identity}, {column} FROM {table} WHERE {column} IS NOT NULL ORDER BY {column} DESC, {identity} ASC LIMIT 5".format(
            identity=identity, column=column["name"], table=table["name"]
        )
        aggregates.append(
            _base_case(
                question="列出表 {table} 中{label}最高的 5 条记录，返回 {identity} 和 {column}；数值降序、标识升序。".format(
                    table=table["name"], label=_label(column), identity=identity, column=column["name"]
                ),
                sql=sql,
                skeleton="topk:%s:%s:%s" % (table["name"], column["name"], identity),
                category="aggregate_topk",
                difficulty="medium",
                snapshot_id=snapshot_id,
                tables=(table["name"],),
                columns=(qualified, "%s.%s" % (table["name"], identity)),
                ordered=True,
            )
        )

    nulls = []
    for table, column in nullable_fields:
        qualified = "%s.%s" % (table["name"], column["name"])
        nulls.append(
            _base_case(
                question="表 {table} 中{label}（{column}）为 NULL 的记录有多少条？".format(
                    table=table["name"], label=_label(column), column=column["name"]
                ),
                sql="SELECT COUNT(*) AS result_count FROM {table} WHERE {column} IS NULL".format(
                    table=table["name"], column=column["name"]
                ),
                skeleton="null_count:%s:%s" % (table["name"], column["name"]),
                category="null_existence",
                difficulty="easy",
                snapshot_id=snapshot_id,
                tables=(table["name"],),
                columns=(qualified,),
            )
        )
        nulls.append(
            _base_case(
                question="表 {table} 是否存在{label}（{column}）非空的记录？存在返回 1，否则返回 0。".format(
                    table=table["name"], label=_label(column), column=column["name"]
                ),
                sql="SELECT CASE WHEN EXISTS (SELECT 1 FROM {table} WHERE {column} IS NOT NULL) THEN 1 ELSE 0 END AS result_exists".format(
                    table=table["name"], column=column["name"]
                ),
                skeleton="nonnull_exists:%s:%s" % (table["name"], column["name"]),
                category="null_existence",
                difficulty="medium",
                snapshot_id=snapshot_id,
                tables=(table["name"],),
                columns=(qualified,),
            )
        )

    joins = []
    relationships = sorted(
        (
            item for item in join_catalog.get("relationships", ())
            if item["left"].split(".", 1)[0] in by_table
            and item["right"].split(".", 1)[0] in by_table
            and int((item.get("data_overlap") or {}).get("intersection_count", 0)) > 0
        ),
        key=lambda item: (
            -float(item.get("confidence", 0)),
            -int((item.get("data_overlap") or {}).get("intersection_count", 0)),
            item["candidate_id"],
        ),
    )
    for relation in relationships:
        left_table, left_column = relation["left"].split(".", 1)
        right_table, right_column = relation["right"].split(".", 1)
        sql = (
            "SELECT COUNT(*) AS joined_rows FROM {left_table} AS l INNER JOIN {right_table} AS r "
            "ON l.{left_column} = r.{right_column}"
        ).format(
            left_table=left_table,
            right_table=right_table,
            left_column=left_column,
            right_column=right_column,
        )
        joins.append(
            _base_case(
                question=(
                    "将表 {left_table} 与 {right_table} 按 {left} = {right} 内连接，"
                    "连接后共有多少行？"
                ).format(
                    left_table=left_table,
                    right_table=right_table,
                    left=relation["left"],
                    right=relation["right"],
                ),
                sql=sql,
                skeleton="join_count:%s" % relation["candidate_id"],
                category="join",
                difficulty="hard",
                snapshot_id=snapshot_id,
                tables=(left_table, right_table),
                columns=(relation["left"], relation["right"]),
                relationships=("join:%s" % relation["candidate_id"],),
            )
        )

    composites = []
    values_by_table = defaultdict(list)
    for table, column, values in value_fields:
        values_by_table[table["name"]].append((table, column, values))
    for table_name, fields in sorted(values_by_table.items()):
        for index, (left_table, left_column, left_values) in enumerate(fields):
            for right_table, right_column, right_values in fields[index + 1 :]:
                left_value, right_value = left_values[0], right_values[0]
                identity = _identifier(left_table)
                sql = (
                    "SELECT COUNT(DISTINCT {identity}) AS result_count FROM {table} "
                    "WHERE {left_column} = {left_value} AND {right_column} = {right_value}"
                ).format(
                    identity=identity,
                    table=table_name,
                    left_column=left_column["name"],
                    left_value=_literal(left_value),
                    right_column=right_column["name"],
                    right_value=_literal(right_value),
                )
                composites.append(
                    _base_case(
                        question=(
                            "表 {table} 中同时满足{left_label}=“{left_value}”且"
                            "{right_label}=“{right_value}”的不同 {identity} 有多少个？"
                        ).format(
                            table=table_name,
                            left_label=_label(left_column),
                            left_value=left_value,
                            right_label=_label(right_column),
                            right_value=right_value,
                            identity=identity,
                        ),
                        sql=sql,
                        skeleton="two_filter_count:%s:%s:%s" % (
                            table_name, left_column["name"], right_column["name"]
                        ),
                        category="composite_subquery",
                        difficulty="hard",
                        snapshot_id=snapshot_id,
                        tables=(table_name,),
                        columns=(
                            "%s.%s" % (table_name, identity),
                            "%s.%s" % (table_name, left_column["name"]),
                            "%s.%s" % (table_name, right_column["name"]),
                        ),
                    )
                )
    for table, column in numeric_fields:
        qualified = "%s.%s" % (table["name"], column["name"])
        sql = (
            "SELECT COUNT(*) AS result_count FROM {table} WHERE {column} > "
            "(SELECT AVG({column}) FROM {table} WHERE {column} IS NOT NULL)"
        ).format(table=table["name"], column=column["name"])
        composites.append(
            _base_case(
                question="表 {table} 中{label}高于该字段非空平均值的记录有多少条？".format(
                    table=table["name"], label=_label(column)
                ),
                sql=sql,
                skeleton="above_average:%s:%s" % (table["name"], column["name"]),
                category="composite_subquery",
                difficulty="hard",
                snapshot_id=snapshot_id,
                tables=(table["name"],),
                columns=(qualified,),
            )
        )

    pools = {
        "projection_filter": projection,
        "count_group": counts,
        "aggregate_topk": aggregates,
        "null_existence": nulls,
        "join": joins,
        "composite_subquery": composites,
    }
    generated = []
    for category, target in CATEGORY_TARGETS.items():
        selected = _take_unique(pools[category], target)
        if any(case["category"] != category for case in selected):
            raise AssertionError("generated category mismatch")
        generated.extend(selected)
    if len(generated) != 240 or len({case["case_id"] for case in generated}) != 240:
        raise AssertionError("dataset generator must produce 240 unique cases")
    return generated


def _choose_groups(groups: Sequence[tuple[str, list[dict[str, Any]]]], target: int):
    reachable: dict[int, tuple[int, ...]] = {0: ()}
    for index, (_, cases) in enumerate(groups):
        size = len(cases)
        for current, chosen in list(sorted(reachable.items(), reverse=True)):
            total = current + size
            if total <= target and total not in reachable:
                reachable[total] = chosen + (index,)
    if target not in reachable:
        raise ValueError("cannot split skeleton groups at exact target %d" % target)
    selected = set(reachable[target])
    return [group for index, group in enumerate(groups) if index in selected], [
        group for index, group in enumerate(groups) if index not in selected
    ]


def assign_splits(cases: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    by_category = defaultdict(lambda: defaultdict(list))
    for case in cases:
        by_category[case["category"]][case["sql_skeleton"]].append(case)
    for category, category_cases in by_category.items():
        groups = sorted(
            category_cases.items(),
            key=lambda item: hashlib.sha256(
                ("text2sql-v1-split:" + item[0]).encode("utf-8")
            ).hexdigest(),
        )
        total = sum(len(values) for _, values in groups)
        train_groups, remaining = _choose_groups(groups, int(total * 0.6))
        validation_groups, holdout_groups = _choose_groups(remaining, int(total * 0.2))
        for split, selected in (
            ("train", train_groups),
            ("validation", validation_groups),
            ("sealed_holdout", holdout_groups),
        ):
            for _, values in selected:
                for case in values:
                    case["split"] = split
    skeleton_splits = defaultdict(set)
    for case in cases:
        skeleton_splits[case["sql_skeleton"]].add(case["split"])
    if any(len(values) != 1 for values in skeleton_splits.values()):
        raise AssertionError("one SQL skeleton leaked across dataset splits")
    return list(cases)


def build_dataset(
    snapshot: Mapping[str, Any],
    join_catalog: Mapping[str, Any],
    database_path: Path,
    output_root: Path,
) -> Mapping[str, Any]:
    cases = assign_splits(generate_cases(snapshot, join_catalog))
    executor = ReadOnlySQLiteExecutor(database_path, snapshot, max_rows=10_000, timeout_ms=10_000)
    for case in cases:
        gate = validate_sql(case["gold_sql"], snapshot)
        if not gate.accepted:
            raise ValueError("generated Gold SQL failed safety gate: %s %s" % (case["case_id"], gate.errors))
        result = executor.execute(case["gold_sql"])
        if result.truncated:
            raise ValueError("generated Gold result was truncated: %s" % case["case_id"])
        case["gold_result_fingerprint"] = result_fingerprint(
            result.columns, result.rows, case["ordered"]
        )
        case["gold_row_count"] = result.row_count
        case["gold_column_count"] = len(result.columns)

    output_root.mkdir(parents=True, exist_ok=True)
    files = {}
    for split in ("train", "validation", "sealed_holdout"):
        path = output_root / (split + ".jsonl")
        values = sorted((case for case in cases if case["split"] == split), key=lambda item: item["case_id"])
        path.write_text(
            "".join(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n" for case in values),
            encoding="utf-8",
        )
        files[split] = {
            "path": path.name,
            "case_count": len(values),
            "sha256": sha256_file(path),
        }
    fingerprint_payload = {
        "contract_version": 1,
        "database_snapshot_id": snapshot["snapshot_id"],
        "files": {split: files[split]["sha256"] for split in sorted(files)},
    }
    dataset_sha = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest = {
        "contract_version": 1,
        "dataset_id": "text2sql-eval-v1-%s" % dataset_sha[:16],
        "dataset_sha256": dataset_sha,
        "database_snapshot_id": snapshot["snapshot_id"],
        "source": "original deterministic generation from the copied database snapshot only",
        "review_status": "machine_validated_pending_human_review",
        "human_reviewed_cases": 0,
        "release_eligible": False,
        "category_counts": dict(CATEGORY_TARGETS),
        "files": files,
        "split_policy": "SQL skeleton grouped 60/20/20; Gold never enters agent input",
    }
    (output_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest
