import json
from pathlib import Path

from evoagent.text2sql.schema_linking import build_draft_link_pack, parse_draft_sql


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = json.loads(
    (PROJECT_ROOT / "artifacts" / "text2sql" / "schema" / "database_snapshot.json").read_text(
        encoding="utf-8"
    )
)


def test_draft_ast_recovers_alias_columns_without_execution():
    linked = parse_draft_sql(
        "SELECT a.d_sumEvent FROM t_activeinfo AS a "
        "WHERE a.c_energy = 4986000 ORDER BY a.d_sumEvent LIMIT 50",
        "查询能量对应的累计事件",
        SNAPSHOT,
    )
    assert linked["valid"] is True
    assert linked["tables"] == ["t_activeinfo"]
    assert set(linked["columns"]) == {
        "t_activeinfo.d_sumEvent",
        "t_activeinfo.c_energy",
    }
    assert linked["projection_columns"] == ["t_activeinfo.d_sumEvent"]


def test_draft_ast_reports_clause_columns_unresolved_names_and_star():
    linked = parse_draft_sql(
        "SELECT a.d_sumEvent, a.missing_metric FROM t_activeinfo AS a "
        "WHERE a.c_energy > 10 GROUP BY a.d_sumEvent "
        "ORDER BY a.d_sumEventRate DESC",
        "查询累计事件",
        SNAPSHOT,
    )
    assert linked["filter_columns"] == ["t_activeinfo.c_energy"]
    assert linked["group_columns"] == ["t_activeinfo.d_sumEvent"]
    assert linked["order_columns"] == ["t_activeinfo.d_sumEventRate"]
    assert linked["join_columns"] == []
    assert linked["unresolved_columns"] == ["a.missing_metric"]
    assert linked["ambiguous_columns"] == []
    assert linked["has_star"] is False

    star = parse_draft_sql("SELECT * FROM t_activeinfo", "查询全部", SNAPSHOT)
    assert star["has_star"] is True


def test_draft_ast_preserves_ambiguous_column_and_all_snapshot_owners():
    linked = parse_draft_sql(
        "SELECT c_caseCode FROM t_harm h JOIN t_support s "
        "ON h.c_caseCode=s.c_caseCode",
        "查询危害和支护信息",
        SNAPSHOT,
    )
    ambiguous = linked["ambiguous_columns"][0]
    assert ambiguous["identifier"] == "c_caseCode"
    assert set(ambiguous["candidates"]) == {
        "t_harm.c_caseCode",
        "t_support.c_caseCode",
    }
    assert set(linked["join_columns"]) == {
        "t_harm.c_caseCode",
        "t_support.c_caseCode",
    }
    assert {
        "t_harm.c_caseCode",
        "t_support.c_caseCode",
        "t_caseinfo.c_caseCode",
    }.issubset(set(linked["column_owners"]["c_caseCode"]))


def test_question_and_draft_union_adds_missing_field_and_full_ddl():
    pack = build_draft_link_pack(
        "在表 t_activeinfo 中，列出能量值等于“4986000.00”的d_sumEvent，最多 50 条。",
        SNAPSHOT,
        draft_sql="SELECT d_sumEvent FROM t_activeinfo WHERE c_energy=4986000 LIMIT 50",
    )
    assert set(pack["columns"]) >= {
        "t_activeinfo.d_sumEvent",
        "t_activeinfo.c_energy",
    }
    assert pack["full_ddl"][0]["table"] == "t_activeinfo"
    assert '"d_sumEvent"' in pack["full_ddl"][0]["ddl"]
    assert '"c_energy"' in pack["full_ddl"][0]["ddl"]


def test_draft_column_reverse_lookup_adds_every_owner_and_full_ddl():
    pack = build_draft_link_pack(
        "查询危害记录",
        SNAPSHOT,
        draft_sql="SELECT c_caseCode FROM t_harm",
        max_tables=50,
    )
    owners = set(pack["column_owners"]["c_caseCode"])
    assert owners.issubset(set(pack["columns"]))
    assert {value.split(".", 1)[0] for value in owners}.issubset(set(pack["tables"]))
    assert set(pack["tables"]) == {
        item["table"] for item in pack["full_ddl"]
    }
    expanded = next(
        item
        for item in pack["links"]
        if item["identifier"] == "t_caseinfo.c_caseCode"
    )
    assert expanded["sources"] == ["snapshot_expansion"]
    assert pack["coverage"]["snapshot_expanded"] is True


def test_forward_llm_and_keyword_candidates_are_snapshot_checked_and_traced():
    pack = build_draft_link_pack(
        "查询候选字段",
        SNAPSHOT,
        forward_candidates={
            "tables": ["t_activeinfo", "t_does_not_exist"],
            "columns": [
                {
                    "identifier": "t_activeinfo.d_event",
                    "source": "llm_forward",
                    "logical_name": "当前事件数",
                },
                {"identifier": "c_rockEnergy", "source": "keyword_match"},
                {"identifier": "t_activeinfo.not_a_column", "source": "llm_forward"},
            ],
            "logical_concepts": [
                {
                    "logical_name": "能量",
                    "column": "t_activeinfoevent.c_rockEnergy",
                    "source": "keyword_match",
                }
            ],
        },
        max_tables=50,
    )
    assert "t_does_not_exist" not in pack["tables"]
    assert "t_activeinfo.not_a_column" not in pack["columns"]

    links = {item["identifier"]: item for item in pack["links"]}
    assert "llm_forward" in links["t_activeinfo.d_event"]["sources"]
    assert {
        "keyword_match",
        "snapshot_expansion",
    }.issubset(set(links["t_activeinfoevent.c_rockEnergy"]["sources"]))
    assert {
        "t_activeevent.c_rockEnergy",
        "t_activeinfoevent.c_rockEnergy",
    }.issubset(set(pack["column_owners"]["c_rockEnergy"]))
    assert any(
        item["logical_name"] == "能量"
        and item["column"] == "t_activeinfoevent.c_rockEnergy"
        for item in pack["logical_concepts"]
    )
    ddl_by_table = {item["table"]: item["ddl"] for item in pack["full_ddl"]}
    assert '"d_event"' in ddl_by_table["t_activeinfo"]
    assert '"c_rockEnergy"' in ddl_by_table["t_activeinfoevent"]


def test_exact_question_terms_create_a_deterministic_logical_concept_manifest():
    pack = build_draft_link_pack(
        "表 t_activeinfo 是否存在当前事件数量（d_event）非空的记录？",
        SNAPSHOT,
    )
    concepts = {item["column"]: item for item in pack["logical_concepts"]}
    assert concepts["t_activeinfo.d_event"]["logical_name"] == "当前事件数量"
    assert "d_event" in concepts["t_activeinfo.d_event"]["aliases"]


def test_numeric_profile_value_preserves_surface_form_and_uses_sqlite_affinity():
    pack = build_draft_link_pack(
        "表 t_activeinfo 中累计事件增速等于“19.00”",
        SNAPSHOT,
    )
    linked = next(
        item
        for item in pack["value_links"]
        if item["column"] == "t_activeinfo.d_sumEventRate"
    )
    assert linked["logical_value"] == "19.00"
    assert linked["physical_value"] == 19.0


def test_explicit_join_is_distinct_from_draft_inference():
    sql = (
        "SELECT h.c_caseCode FROM t_harm h JOIN t_support s "
        "ON h.c_caseCode=s.c_caseCode"
    )
    explicit = build_draft_link_pack(
        "按 t_harm.c_caseCode = t_support.c_caseCode 连接两个表",
        SNAPSHOT,
        draft_sql=sql,
    )
    inferred = build_draft_link_pack(
        "查询危害和支护信息",
        SNAPSHOT,
        draft_sql=sql,
    )
    assert explicit["joins"][0]["source"] == "user_explicit"
    assert inferred["joins"][0]["source"] == "draft_inferred"


def test_reviewed_240_cases_have_deterministic_schema_coverage():
    cases = []
    for path in (PROJECT_ROOT / "evaluation" / "datasets" / "text2sql_v1").glob("*.jsonl"):
        cases.extend(
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    assert len(cases) == 240
    for case in cases:
        pack = build_draft_link_pack(case["question"], SNAPSHOT)
        assert set(case["required_tables"]).issubset(pack["tables"]), case["case_id"]
        assert set(case["required_columns"]).issubset(pack["columns"]), case["case_id"]
        if case["category"] == "join":
            assert any(item["source"] == "user_explicit" for item in pack["joins"]), case[
                "case_id"
            ]
