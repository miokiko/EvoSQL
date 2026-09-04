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
