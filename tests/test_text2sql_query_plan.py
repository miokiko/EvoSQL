import hashlib
import json
import unittest

from evoagent.text2sql.contracts import (
    ApprovedQueryPlan,
    BoundQueryPlan,
    PlanBinding,
    QueryMeasure,
    QuerySpec,
    SQLCandidate,
    SchemaBinding,
    SchemaPlan,
    SchemaValueBinding,
)
from evoagent.text2sql.query_plan import (
    QueryPlanBindingError,
    approve_query_plan,
    bind_query_plan,
    check_candidate_conformance,
    check_plan_conformance,
)


SNAPSHOT = {
    "snapshot_id": "snapshot-v1",
    "tables": [
        {
            "name": "t_case",
            "columns": [
                {"name": "c_id"},
                {"name": "c_category"},
            ],
        },
        {
            "name": "t_detail",
            "columns": [
                {"name": "c_case_id"},
                {"name": "c_level"},
            ],
        },
    ],
}


def grouped_query_spec():
    return QuerySpec.from_dict(
        {
            "intent": "ranking",
            "subject": "按类别统计强烈案例数",
            "dimensions": [{"slot_id": "dimension-category", "concept": "案例类别"}],
            "measures": [
                {
                    "slot_id": "measure-count",
                    "name": "案例数",
                    "aggregation": "count",
                    "field_concept": "案例ID",
                    "distinct": True,
                }
            ],
            "filters": [
                {
                    "slot_id": "filter-level",
                    "field_concept": "岩爆等级",
                    "operator": "eq",
                    "value": "强烈",
                }
            ],
            "order_by": [
                {
                    "slot_id": "order-count",
                    "target": "measure-count",
                    "direction": "desc",
                }
            ],
            "limit": 3,
            "expected_shape": "grouped_rows",
            "version": 2,
        }
    )


def grounded_schema_plan():
    return SchemaPlan.from_dict(
        {
            "tables": ["t_case", "t_detail"],
            "columns": [
                "t_case.c_id",
                "t_case.c_category",
                "t_detail.c_case_id",
                "t_detail.c_level",
            ],
            "joins": [
                {
                    "left": "t_case.c_id",
                    "right": "t_detail.c_case_id",
                    "type": "inner",
                    "source": "stable",
                    "evidence_id": "join:case-detail",
                }
            ],
            "result_grain": ["t_case.c_category"],
            "bindings": [
                {
                    "logical_name": "案例类别",
                    "column": "t_case.c_category",
                    "aliases": ["类别"],
                    "evidence_ids": ["schema:case-category"],
                },
                {
                    "logical_name": "案例ID",
                    "column": "t_case.c_id",
                    "evidence_ids": ["schema:case-id"],
                },
                {
                    "logical_name": "岩爆等级",
                    "column": "t_detail.c_level",
                    "evidence_ids": ["schema:detail-level"],
                    "value_bindings": [
                        {
                            "logical_value": "强烈",
                            "physical_value": "强烈",
                            "evidence_ids": ["value:detail-level:strong"],
                        }
                    ],
                },
            ],
        }
    )


VALID_SQL = (
    "SELECT c.c_category, COUNT(DISTINCT c.c_id) AS case_count "
    "FROM t_case AS c JOIN t_detail AS d ON c.c_id = d.c_case_id "
    "WHERE d.c_level = '强烈' GROUP BY c.c_category "
    "ORDER BY case_count DESC LIMIT 3"
)


class QueryPlanContractTests(unittest.TestCase):
    def test_plural_string_contracts_reject_scalar_and_mapping_inputs(self):
        for invalid in ("alias", b"alias", {"alias": "value"}):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "must be a sequence"):
                    SchemaBinding(
                        logical_name="案例ID",
                        column="t_case.c_id",
                        aliases=invalid,
                    )

        with self.assertRaisesRegex(ValueError, "must be a sequence"):
            SchemaValueBinding(
                logical_value="强烈",
                physical_value="强烈",
                evidence_ids="value:level:strong",
            )
        with self.assertRaisesRegex(ValueError, "must be a sequence"):
            SchemaPlan.from_dict(
                {
                    "tables": {"t_case": True},
                    "columns": ["t_case.c_id"],
                }
            )

        binding = SchemaBinding(
            logical_name="案例ID",
            column="t_case.c_id",
            aliases=["编号"],
            evidence_ids=["schema:case-id"],
        )
        self.assertEqual(binding.aliases, ("编号",))
        self.assertEqual(binding.evidence_ids, ("schema:case-id",))

    def test_count_all_requires_a_native_boolean(self):
        for invalid in ("false", 0, 1, None):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "count_all must be boolean"):
                    QueryMeasure(
                        slot_id="measure-count",
                        name="案例数",
                        aggregation="count",
                        count_all=invalid,
                    )

        measure = QueryMeasure.from_value(
            {
                "slot_id": "measure-count",
                "name": "案例数",
                "aggregation": "count",
                "count_all": False,
                "field_concept": "案例ID",
            }
        )
        self.assertIs(measure.count_all, False)

    def test_query_spec_limit_and_version_require_strict_integers(self):
        for field_name, invalid in (
            ("limit", True),
            ("limit", "20"),
            ("limit", 20.0),
            ("version", True),
            ("version", "1"),
            ("version", 1.0),
        ):
            with self.subTest(field=field_name, invalid=invalid):
                value = {
                    "intent": "lookup",
                    "subject": "案例",
                    "dimensions": ["案例ID"],
                    field_name: invalid,
                }
                with self.assertRaisesRegex(ValueError, "must be an integer"):
                    QuerySpec.from_dict(value)

        spec = QuerySpec.from_dict(
            {
                "intent": "lookup",
                "subject": "案例",
                "dimensions": ["案例ID"],
                "limit": 20,
                "version": 1,
            }
        )
        self.assertEqual((spec.limit, spec.version), (20, 1))

    def test_query_spec_preserves_legacy_strings_and_typed_mapping_dimensions(self):
        legacy = QuerySpec.from_dict(
            {
                "intent": "lookup",
                "subject": "案例",
                "dimensions": ["t_case.c_id"],
            }
        )
        self.assertEqual(legacy.dimension_specs()[0].concept, "t_case.c_id")

        typed = grouped_query_spec()
        self.assertIsInstance(typed.dimensions[0], dict)
        self.assertEqual(typed.dimension_specs()[0].slot_id, "dimension-category")
        self.assertTrue(typed.measure_specs()[0].distinct)
        self.assertEqual(typed.filter_specs()[0].operator, "eq")

    def test_schema_plan_accepts_canonical_and_mapping_bindings(self):
        plan = SchemaPlan.from_dict(
            {
                "tables": ["t_case"],
                "columns": ["t_case.c_id"],
                "field_bindings": {"案例ID": "t_case.c_id"},
            }
        )
        self.assertEqual(plan.bindings[0].logical_name, "案例ID")
        self.assertEqual(plan.as_dict()["bindings"][0]["column"], "t_case.c_id")

    def test_binding_is_complete_fingerprinted_and_checkpoint_round_trips(self):
        bound = bind_query_plan(
            grouped_query_spec(),
            grounded_schema_plan(),
            version_pins={"database_snapshot_id": "snapshot-v1", "policy_version": "policy-v2"},
        )
        self.assertEqual(len(bound.bindings), 4)
        self.assertEqual(len(bound.fingerprint), 64)
        self.assertEqual(BoundQueryPlan.from_dict(bound.as_dict()), bound)

    def test_row_lookup_allows_a_grain_key_subset_of_projected_dimensions(self):
        spec = QuerySpec.from_dict(
            {
                "intent": "lookup",
                "subject": "案例编号和类别",
                "dimensions": [
                    {"slot_id": "dimension-id", "concept": "案例ID"},
                    {"slot_id": "dimension-category", "concept": "案例类别"},
                ],
                "expected_shape": "rows",
            }
        )
        plan = SchemaPlan.from_dict(
            {
                "tables": ["t_case"],
                "columns": ["t_case.c_id", "t_case.c_category"],
                "result_grain": ["t_case.c_id"],
                "bindings": [
                    {"logical_name": "案例ID", "column": "t_case.c_id"},
                    {
                        "logical_name": "案例类别",
                        "column": "t_case.c_category",
                    },
                ],
            }
        )
        bound = bind_query_plan(spec, plan)
        self.assertEqual(
            tuple(
                item.column for item in bound.bindings if item.kind == "dimension"
            ),
            ("t_case.c_id", "t_case.c_category"),
        )

        approved = approve_query_plan(
            bound,
            approval_reason="The bound semantics match the user question.",
            approval_id="lead-decision-1",
        )
        self.assertIsInstance(approved, ApprovedQueryPlan)
        self.assertEqual(ApprovedQueryPlan.from_dict(approved.as_dict()), approved)

        tampered = json.loads(json.dumps(bound.as_dict(), ensure_ascii=False))
        tampered["query_spec"]["limit"] = 9
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            BoundQueryPlan.from_dict(tampered)

    def test_row_join_rejects_grain_key_outside_projected_dimensions(self):
        spec = QuerySpec.from_dict(
            {
                "intent": "lookup",
                "subject": "案例类别",
                "dimensions": [
                    {"slot_id": "dimension-category", "concept": "案例类别"}
                ],
                "expected_shape": "rows",
            }
        )
        schema = grounded_schema_plan().as_dict()
        schema["result_grain"] = ["t_case.c_id"]

        with self.assertRaises(QueryPlanBindingError) as caught:
            bind_query_plan(spec, SchemaPlan.from_dict(schema))

        self.assertIn(
            "result_grain_mismatch",
            {item.code for item in caught.exception.conflicts},
        )

        # A bound plan restored from an older checkpoint must also fail at the
        # final conformance boundary even if it predates the binder invariant.
        unsafe_bound = BoundQueryPlan(
            query_spec=spec,
            schema_plan=SchemaPlan.from_dict(schema),
            bindings=(
                PlanBinding(
                    slot_id="dimension-category",
                    kind="dimension",
                    logical_name="案例类别",
                    column="t_case.c_category",
                    evidence_ids=("schema:case-category",),
                ),
            ),
        )
        result = check_plan_conformance(
            "SELECT c.c_category FROM t_case AS c "
            "JOIN t_detail AS d ON c.c_id = d.c_case_id LIMIT 20",
            unsafe_bound,
            SNAPSHOT,
        )
        self.assertFalse(result.accepted)
        self.assertIn("result_grain_mismatch", result.errors)

    def test_row_join_rejects_empty_result_grain(self):
        spec = QuerySpec.from_dict(
            {
                "intent": "lookup",
                "subject": "案例类别",
                "dimensions": [
                    {"slot_id": "dimension-category", "concept": "案例类别"}
                ],
                "expected_shape": "rows",
            }
        )
        schema = grounded_schema_plan().as_dict()
        schema["result_grain"] = []
        plan = SchemaPlan.from_dict(schema)

        with self.assertRaises(QueryPlanBindingError) as caught:
            bind_query_plan(spec, plan)
        self.assertIn(
            "result_grain_mismatch",
            {item.code for item in caught.exception.conflicts},
        )

        unsafe_bound = BoundQueryPlan(
            query_spec=spec,
            schema_plan=plan,
            bindings=(
                PlanBinding(
                    slot_id="dimension-category",
                    kind="dimension",
                    logical_name="案例类别",
                    column="t_case.c_category",
                    evidence_ids=("schema:case-category",),
                ),
            ),
        )
        result = check_plan_conformance(
            "SELECT c.c_category FROM t_case AS c "
            "JOIN t_detail AS d ON c.c_id = d.c_case_id LIMIT 20",
            unsafe_bound,
            SNAPSHOT,
        )
        self.assertFalse(result.accepted)
        self.assertIn("result_grain_mismatch", result.errors)

    def test_row_join_rejects_projected_grain_without_fanout_contract(self):
        spec = QuerySpec.from_dict(
            {
                "intent": "lookup",
                "subject": "案例编号",
                "dimensions": [
                    {"slot_id": "dimension-id", "concept": "案例ID"}
                ],
                "expected_shape": "rows",
            }
        )
        schema = grounded_schema_plan().as_dict()
        schema["result_grain"] = ["t_case.c_id"]
        plan = SchemaPlan.from_dict(schema)

        with self.assertRaises(QueryPlanBindingError) as caught:
            bind_query_plan(spec, plan)
        self.assertIn(
            "result_grain_mismatch",
            {item.code for item in caught.exception.conflicts},
        )

        unsafe_bound = BoundQueryPlan(
            query_spec=spec,
            schema_plan=plan,
            bindings=(
                PlanBinding(
                    slot_id="dimension-id",
                    kind="dimension",
                    logical_name="案例ID",
                    column="t_case.c_id",
                    evidence_ids=("schema:case-id",),
                ),
            ),
        )
        result = check_plan_conformance(
            "SELECT c.c_id FROM t_case AS c "
            "JOIN t_detail AS d ON c.c_id = d.c_case_id LIMIT 20",
            unsafe_bound,
            SNAPSHOT,
        )
        self.assertFalse(result.accepted)
        self.assertIn("result_grain_mismatch", result.errors)

    def test_binding_fails_closed_for_missing_or_ambiguous_concepts(self):
        missing = grounded_schema_plan().as_dict()
        missing["bindings"] = [
            item for item in missing["bindings"] if item["logical_name"] != "岩爆等级"
        ]
        with self.assertRaises(QueryPlanBindingError) as caught:
            bind_query_plan(grouped_query_spec(), SchemaPlan.from_dict(missing))
        self.assertIn("missing_schema_binding", {item.code for item in caught.exception.conflicts})
        self.assertIn("schema-grounding", {item.owner for item in caught.exception.conflicts})

        ambiguous = grounded_schema_plan().as_dict()
        ambiguous["bindings"] = list(ambiguous["bindings"]) + [
            {"logical_name": "案例ID", "column": "t_detail.c_case_id"}
        ]
        with self.assertRaises(QueryPlanBindingError) as caught:
            bind_query_plan(grouped_query_spec(), SchemaPlan.from_dict(ambiguous))
        self.assertIn("ambiguous_schema_binding", {item.code for item in caught.exception.conflicts})

    def test_measure_requires_explicit_field_and_distinct_semantics(self):
        legacy = QuerySpec.from_dict(
            {
                "intent": "count",
                "subject": "案例数",
                "measures": [{"name": "案例数", "aggregation": "count"}],
                "expected_shape": "scalar",
            }
        )
        with self.assertRaises(QueryPlanBindingError) as caught:
            bind_query_plan(legacy, grounded_schema_plan())
        codes = {item.code for item in caught.exception.conflicts}
        self.assertIn("missing_logical_reference", codes)
        self.assertIn("unsupported_query_contract", codes)

    def test_filter_values_require_explicit_type_preserving_grounding(self):
        schema = grounded_schema_plan().as_dict()
        level_binding = next(
            item for item in schema["bindings"] if item["logical_name"] == "岩爆等级"
        )
        level_binding["value_bindings"] = [
            {
                "logical_value": "强烈",
                "physical_value": 3,
                "evidence_ids": ["value:level:3"],
            }
        ]
        bound = bind_query_plan(grouped_query_spec(), SchemaPlan.from_dict(schema))
        predicate = next(item for item in bound.bindings if item.kind == "filter")
        self.assertEqual(predicate.logical_value, "强烈")
        self.assertEqual(predicate.value, 3)
        self.assertIsInstance(predicate.value, int)

        level_binding["value_bindings"] = []
        with self.assertRaises(QueryPlanBindingError) as caught:
            bind_query_plan(grouped_query_spec(), SchemaPlan.from_dict(schema))
        self.assertIn("missing_value_binding", {item.code for item in caught.exception.conflicts})

    def test_contract_rejects_unrepresentable_count_filter_and_join_shapes(self):
        with self.assertRaisesRegex(ValueError, r"COUNT\(\*\).*DISTINCT"):
            QuerySpec.from_dict(
                {
                    "intent": "count",
                    "subject": "案例数",
                    "measures": [
                        {
                            "slot_id": "measure-count",
                            "name": "案例数",
                            "aggregation": "count",
                            "count_all": True,
                            "distinct": True,
                        }
                    ],
                    "expected_shape": "scalar",
                }
            ).measure_specs()

        with self.assertRaisesRegex(ValueError, "at least one"):
            QuerySpec.from_dict(
                {
                    "intent": "lookup",
                    "subject": "案例",
                    "filters": [
                        {
                            "slot_id": "filter-empty",
                            "field_concept": "案例ID",
                            "operator": "in",
                            "value": [],
                        }
                    ],
                }
            ).filter_specs()

        malformed = grounded_schema_plan().as_dict()
        malformed["joins"] = ["t_case.c_id=t_detail.c_case_id"]
        with self.assertRaisesRegex(ValueError, "join entries"):
            SchemaPlan.from_dict(malformed)

    def test_multitable_schema_plan_requires_connected_non_self_join_graph(self):
        with self.assertRaisesRegex(ValueError, "does not cover"):
            SchemaPlan.from_dict(
                {
                    "tables": ["t_case", "t_detail"],
                    "columns": ["t_case.c_id", "t_detail.c_case_id"],
                }
            )
        with self.assertRaisesRegex(ValueError, "self joins"):
            SchemaPlan.from_dict(
                {
                    "tables": ["t_case"],
                    "columns": ["t_case.c_id", "t_case.c_category"],
                    "joins": [
                        {
                            "left": "t_case.c_id",
                            "right": "t_case.c_category",
                            "type": "inner",
                        }
                    ],
                }
            )

    def test_bound_plan_replays_binding_semantics_after_fingerprint_recompute(self):
        bound = bind_query_plan(grouped_query_spec(), grounded_schema_plan())

        def resign(payload):
            canonical = {
                "contract": "BoundQueryPlan/v1",
                "query_spec": payload["query_spec"],
                "schema_plan": payload["schema_plan"],
                "bindings": payload["bindings"],
                "version_pins": payload["version_pins"],
            }
            payload["fingerprint"] = hashlib.sha256(
                json.dumps(
                    canonical,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()

        mutations = (
            ("measure-count", {"distinct": False}),
            ("filter-level", {"operator": "neq", "value": "中等"}),
            ("order-count", {"direction": "asc"}),
        )
        for slot_id, changes in mutations:
            with self.subTest(slot_id=slot_id):
                payload = json.loads(json.dumps(bound.as_dict(), ensure_ascii=False))
                target = next(
                    item for item in payload["bindings"] if item["slot_id"] == slot_id
                )
                target.update(changes)
                resign(payload)
                with self.assertRaisesRegex(ValueError, "deterministic"):
                    BoundQueryPlan.from_dict(payload)

        forged = list(bound.bindings)
        original = next(item for item in forged if item.slot_id == "measure-count")
        forged[forged.index(original)] = PlanBinding(
            **{**original.as_dict(), "distinct": False}
        )
        with self.assertRaisesRegex(ValueError, "deterministic"):
            BoundQueryPlan(
                bound.query_spec,
                bound.schema_plan,
                forged,
                bound.version_pins,
            )

    def test_binder_rejects_inconsistent_intent_and_result_shape(self):
        spec = QuerySpec.from_dict(
            {
                "intent": "count",
                "subject": "案例数",
                "measures": [
                    {
                        "slot_id": "measure-count",
                        "name": "案例数",
                        "aggregation": "count",
                        "field_concept": "案例ID",
                        "distinct": True,
                    }
                ],
                "expected_shape": "rows",
            }
        )
        with self.assertRaises(QueryPlanBindingError) as caught:
            bind_query_plan(spec, grounded_schema_plan())
        self.assertIn(
            "unsupported_query_contract",
            {item.code for item in caught.exception.conflicts},
        )

    def test_queryspec_v1_rejects_having_scope_before_binding(self):
        spec = grouped_query_spec().as_dict()
        spec["filters"][0]["scope"] = "having"
        with self.assertRaises(QueryPlanBindingError) as caught:
            bind_query_plan(spec, grounded_schema_plan())
        self.assertIn(
            "unsupported_query_contract",
            {item.code for item in caught.exception.conflicts},
        )
        self.assertIn("WHERE filters only", str(caught.exception.__cause__))

    def test_ranking_requires_order_and_a_row_producing_shape(self):
        missing_order = grouped_query_spec().as_dict()
        missing_order["order_by"] = []
        with self.assertRaises(QueryPlanBindingError) as caught:
            bind_query_plan(missing_order, grounded_schema_plan())
        self.assertIn(
            "unsupported_query_contract",
            {item.code for item in caught.exception.conflicts},
        )

        scalar_ranking = grouped_query_spec().as_dict()
        scalar_ranking["dimensions"] = []
        scalar_ranking["expected_shape"] = "scalar"
        with self.assertRaises(QueryPlanBindingError) as caught:
            bind_query_plan(scalar_ranking, grounded_schema_plan())
        self.assertIn(
            "unsupported_query_contract",
            {item.code for item in caught.exception.conflicts},
        )

        grouped_lookup = grouped_query_spec().as_dict()
        grouped_lookup["intent"] = "lookup"
        with self.assertRaises(QueryPlanBindingError) as caught:
            bind_query_plan(grouped_lookup, grounded_schema_plan())
        self.assertIn(
            "unsupported_query_contract",
            {item.code for item in caught.exception.conflicts},
        )


class QueryPlanConformanceTests(unittest.TestCase):
    def setUp(self):
        self.bound = bind_query_plan(grouped_query_spec(), grounded_schema_plan())

    def test_accepts_sql_equivalent_to_bound_plan(self):
        result = check_plan_conformance(VALID_SQL, self.bound, SNAPSHOT)
        self.assertTrue(result.accepted, result.as_dict())
        self.assertEqual(result.errors, ())

    def test_rejects_distinct_filter_order_and_limit_drift(self):
        drifted = (
            "SELECT c.c_category, COUNT(c.c_id) AS case_count "
            "FROM t_case AS c JOIN t_detail AS d ON c.c_id = d.c_case_id "
            "WHERE d.c_level = '中等' GROUP BY c.c_category "
            "ORDER BY case_count ASC LIMIT 5"
        )
        result = check_plan_conformance(drifted, self.bound, SNAPSHOT)
        self.assertFalse(result.accepted)
        self.assertTrue(
            {"distinct_mismatch", "filter_mismatch", "order_by_mismatch", "limit_mismatch"}
            .issubset(set(result.errors))
        )

    def test_rejects_missing_join_and_unprovable_or_filter(self):
        missing_join = (
            "SELECT c_category, COUNT(DISTINCT c_id) AS case_count "
            "FROM t_case WHERE c_category = '强烈' GROUP BY c_category "
            "ORDER BY case_count DESC LIMIT 3"
        )
        result = check_plan_conformance(missing_join, self.bound, SNAPSHOT)
        self.assertIn("missing_table", result.errors)
        self.assertIn("missing_join", result.errors)

        with_or = VALID_SQL.replace(
            "d.c_level = '强烈'",
            "d.c_level = '强烈' OR d.c_level = '极强'",
        )
        result = check_plan_conformance(with_or, self.bound, SNAPSHOT)
        self.assertIn("unsupported_filter_expression", result.errors)

    def test_candidate_must_pin_exact_bound_plan_fingerprint(self):
        pins = {
            "database_snapshot_id": "snapshot-v1",
            "wiki_index_version": "wiki-v1",
            "memory_snapshot_id": "memory-v1",
            "policy_version": "policy-v1",
        }
        unbound = SQLCandidate("candidate-1", VALID_SQL, 2, **pins)
        self.assertEqual(
            check_candidate_conformance(unbound, self.bound, SNAPSHOT).errors,
            ("missing_bound_plan_fingerprint",),
        )
        bound = SQLCandidate(
            "candidate-1",
            VALID_SQL,
            2,
            bound_plan_fingerprint=self.bound.fingerprint,
            **pins,
        )
        self.assertTrue(check_candidate_conformance(bound, self.bound, SNAPSHOT).accepted)

    def test_candidate_must_match_query_and_runtime_versions(self):
        expected_pins = {
            "database_snapshot_id": "snapshot-v1",
            "wiki_index_version": "wiki-v1",
            "vanna_index_version": "vanna-v1",
            "memory_snapshot_id": "memory-v1",
            "policy_version": "policy-v1",
        }
        pinned_plan = bind_query_plan(
            grouped_query_spec(), grounded_schema_plan(), version_pins=expected_pins
        )
        stale = SQLCandidate(
            "candidate-stale",
            VALID_SQL,
            1,
            database_snapshot_id="snapshot-old",
            wiki_index_version="wiki-v1",
            vanna_index_version="vanna-old",
            memory_snapshot_id="memory-v1",
            policy_version="policy-v1",
            bound_plan_fingerprint=pinned_plan.fingerprint,
        )
        result = check_candidate_conformance(stale, pinned_plan, SNAPSHOT)
        self.assertFalse(result.accepted)
        self.assertIn("query_spec_version_mismatch", result.errors)
        self.assertEqual(result.errors.count("version_pin_mismatch"), 2)
        self.assertEqual(
            {item.slot_id for item in result.issues if item.code == "version_pin_mismatch"},
            {"database_snapshot_id", "vanna_index_version"},
        )

    def test_accepts_strict_case_when_exists_scalar_shape(self):
        spec = QuerySpec.from_dict(
            {
                "intent": "existence",
                "subject": "是否存在强烈案例",
                "filters": [
                    {
                        "slot_id": "filter-level",
                        "field_concept": "岩爆等级",
                        "operator": "eq",
                        "value": "强烈",
                    }
                ],
                "expected_shape": "scalar",
            }
        )
        plan = SchemaPlan.from_dict(
            {
                "tables": ["t_detail"],
                "columns": ["t_detail.c_level"],
                "bindings": [
                    {
                        "logical_name": "岩爆等级",
                        "column": "t_detail.c_level",
                        "value_bindings": [
                            {
                                "logical_value": "强烈",
                                "physical_value": "强烈",
                            }
                        ],
                    }
                ],
            }
        )
        bound = bind_query_plan(spec, plan)
        sql = (
            "SELECT CASE WHEN EXISTS (SELECT 1 FROM t_detail "
            "WHERE c_level = '强烈') THEN 1 ELSE 0 END"
        )
        result = check_plan_conformance(sql, bound, SNAPSHOT)
        self.assertTrue(result.accepted, result.as_dict())

        direct = (
            "SELECT EXISTS (SELECT 1 FROM t_detail "
            "WHERE c_level = '强烈')"
        )
        result = check_plan_conformance(direct, bound, SNAPSHOT)
        self.assertTrue(result.accepted, result.as_dict())

    def test_existence_rejects_outer_relational_clauses_and_inner_aggregates(self):
        spec = QuerySpec.from_dict(
            {
                "intent": "existence",
                "subject": "是否存在强烈案例",
                "filters": [
                    {
                        "slot_id": "filter-level",
                        "field_concept": "岩爆等级",
                        "operator": "eq",
                        "value": "强烈",
                    }
                ],
                "expected_shape": "scalar",
            }
        )
        plan = SchemaPlan.from_dict(
            {
                "tables": ["t_detail"],
                "columns": ["t_detail.c_level"],
                "bindings": [
                    {
                        "logical_name": "岩爆等级",
                        "column": "t_detail.c_level",
                        "value_bindings": [
                            {
                                "logical_value": "强烈",
                                "physical_value": "强烈",
                            }
                        ],
                    }
                ],
            }
        )
        bound = bind_query_plan(spec, plan)
        base = (
            "SELECT EXISTS (SELECT 1 FROM t_detail "
            "WHERE c_level = '强烈')"
        )
        for clause in (
            " FROM t_detail",
            " WHERE 1 = 1",
            " GROUP BY 1",
            " HAVING 1 = 1",
        ):
            with self.subTest(outer_clause=clause.strip()):
                result = check_plan_conformance(base + clause, bound, SNAPSHOT)
                self.assertFalse(result.accepted, result.as_dict())
                self.assertIn("result_shape_mismatch", result.errors)

        for projection in ("COUNT(*)", "MAX(c_level)"):
            with self.subTest(inner_projection=projection):
                sql = (
                    "SELECT EXISTS (SELECT %s FROM t_detail "
                    "WHERE c_level = '强烈')" % projection
                )
                result = check_plan_conformance(sql, bound, SNAPSHOT)
                self.assertFalse(result.accepted, result.as_dict())
                self.assertIn("result_shape_mismatch", result.errors)

    def test_rejects_cartesian_repeated_and_extra_on_joins(self):
        cartesian = (
            "SELECT c.c_category, COUNT(DISTINCT c.c_id) AS case_count "
            "FROM t_case AS c {join} t_detail AS d "
            "WHERE d.c_level = '强烈' GROUP BY c.c_category "
            "ORDER BY case_count DESC LIMIT 3"
        )
        for syntax in ("CROSS JOIN", ","):
            with self.subTest(syntax=syntax):
                result = check_plan_conformance(
                    cartesian.format(join=syntax), self.bound, SNAPSHOT
                )
                self.assertFalse(result.accepted)
                self.assertIn("unsupported_join", result.errors)

        extra_on = VALID_SQL.replace(
            "ON c.c_id = d.c_case_id",
            "ON c.c_id = d.c_case_id AND d.c_level = '强烈'",
        )
        result = check_plan_conformance(extra_on, self.bound, SNAPSHOT)
        self.assertFalse(result.accepted)
        self.assertIn("unsupported_join", result.errors)

        repeated = VALID_SQL.replace(
            "WHERE d.c_level",
            "JOIN t_detail AS d2 ON c.c_id = d2.c_case_id WHERE d.c_level",
        )
        result = check_plan_conformance(repeated, self.bound, SNAPSHOT)
        self.assertFalse(result.accepted)
        self.assertIn("unsupported_join", result.errors)

    def test_rejects_offset_and_non_unit_scalar_limit(self):
        result = check_plan_conformance(VALID_SQL + " OFFSET 1", self.bound, SNAPSHOT)
        self.assertFalse(result.accepted)
        self.assertIn("offset_not_allowed", result.errors)

        spec = QuerySpec.from_dict(
            {
                "intent": "count",
                "subject": "案例数",
                "measures": [
                    {
                        "slot_id": "measure-count",
                        "name": "案例数",
                        "aggregation": "count",
                        "count_all": True,
                        "distinct": False,
                    }
                ],
                "expected_shape": "scalar",
            }
        )
        plan = SchemaPlan.from_dict(
            {"tables": ["t_case"], "columns": ["t_case.c_id"]}
        )
        bound = bind_query_plan(spec, plan)
        self.assertTrue(
            check_plan_conformance("SELECT COUNT(*) FROM t_case", bound, SNAPSHOT).accepted
        )
        self.assertTrue(
            check_plan_conformance(
                "SELECT COUNT(*) FROM t_case LIMIT 1", bound, SNAPSHOT
            ).accepted
        )
        result = check_plan_conformance(
            "SELECT COUNT(*) FROM t_case LIMIT 0", bound, SNAPSHOT
        )
        self.assertFalse(result.accepted)
        self.assertIn("limit_mismatch", result.errors)

    def test_projection_order_and_row_distinct_are_contract_bound(self):
        reordered = VALID_SQL.replace(
            "SELECT c.c_category, COUNT(DISTINCT c.c_id) AS case_count",
            "SELECT COUNT(DISTINCT c.c_id) AS case_count, c.c_category",
        )
        result = check_plan_conformance(reordered, self.bound, SNAPSHOT)
        self.assertFalse(result.accepted)
        self.assertIn("result_shape_mismatch", result.errors)

        row_distinct = VALID_SQL.replace("SELECT ", "SELECT DISTINCT ", 1)
        result = check_plan_conformance(row_distinct, self.bound, SNAPSHOT)
        self.assertFalse(result.accepted)
        self.assertIn("unexpected_row_distinct", result.errors)

    def test_duplicate_output_aliases_fail_closed(self):
        duplicate = VALID_SQL.replace(
            "c.c_category, COUNT(DISTINCT c.c_id) AS case_count",
            "c.c_category AS duplicate, "
            "COUNT(DISTINCT c.c_id) AS duplicate",
        ).replace("ORDER BY case_count", "ORDER BY duplicate")
        result = check_plan_conformance(duplicate, self.bound, SNAPSHOT)
        self.assertFalse(result.accepted)
        self.assertIn("duplicate_output_alias", result.errors)

    def test_sqlite_output_alias_folding_is_ascii_only(self):
        snapshot = {
            "tables": [
                {
                    "name": "t_metric",
                    "columns": [
                        {"name": "group_key"},
                        {"name": "s"},
                        {"name": "v"},
                    ],
                }
            ]
        }
        schema = SchemaPlan.from_dict(
            {
                "tables": ["t_metric"],
                "columns": [
                    "t_metric.group_key",
                    "t_metric.s",
                    "t_metric.v",
                ],
                "result_grain": ["t_metric.s"],
                "bindings": [
                    {"logical_name": "分组", "column": "t_metric.s"},
                    {"logical_name": "数值", "column": "t_metric.v"},
                ],
            }
        )

        def ranking(order_target):
            return bind_query_plan(
                QuerySpec.from_dict(
                    {
                        "intent": "ranking",
                        "subject": "分组计数排序",
                        "dimensions": [
                            {"slot_id": "dimension-s", "concept": "分组"}
                        ],
                        "measures": [
                            {
                                "slot_id": "measure-count",
                                "name": "数量",
                                "aggregation": "count",
                                "field_concept": "数值",
                                "distinct": False,
                            }
                        ],
                        "order_by": [
                            {
                                "slot_id": "order-result",
                                "target": order_target,
                                "direction": "desc",
                            }
                        ],
                        "limit": 20,
                        "expected_shape": "grouped_rows",
                    }
                ),
                schema,
            )

        unicode_alias_sql = (
            'SELECT s, COUNT(v) AS "ſ" FROM t_metric '
            "GROUP BY s ORDER BY s DESC LIMIT 20"
        )
        physical_order = ranking("dimension-s")
        aggregate_order = ranking("measure-count")
        result = check_plan_conformance(
            unicode_alias_sql, physical_order, snapshot
        )
        self.assertTrue(result.accepted, result.as_dict())
        self.assertIn(
            "order_by_mismatch",
            check_plan_conformance(
                unicode_alias_sql, aggregate_order, snapshot
            ).errors,
        )

        ascii_alias_sql = (
            'SELECT s, COUNT(v) AS "S" FROM t_metric '
            "GROUP BY s ORDER BY s DESC LIMIT 20"
        )
        result = check_plan_conformance(
            ascii_alias_sql, aggregate_order, snapshot
        )
        self.assertTrue(result.accepted, result.as_dict())

    def test_output_alias_skip_is_limited_to_order_by_reference(self):
        snapshot = {
            "tables": [
                {
                    "name": "t_metric",
                    "columns": [
                        {"name": "group_key"},
                        {"name": "s"},
                        {"name": "v"},
                    ],
                }
            ]
        }
        spec = QuerySpec.from_dict(
            {
                "intent": "ranking",
                "subject": "筛选后计数排序",
                "dimensions": [
                    {"slot_id": "dimension-group", "concept": "分组"}
                ],
                "measures": [
                    {
                        "slot_id": "measure-count",
                        "name": "数量",
                        "aggregation": "count",
                        "field_concept": "数值",
                        "distinct": False,
                    }
                ],
                "filters": [
                    {
                        "slot_id": "filter-s",
                        "field_concept": "筛选列",
                        "operator": "eq",
                        "value": "x",
                    }
                ],
                "order_by": [
                    {
                        "slot_id": "order-count",
                        "target": "measure-count",
                        "direction": "desc",
                    }
                ],
                "limit": 20,
                "expected_shape": "grouped_rows",
            }
        )
        schema = SchemaPlan.from_dict(
            {
                "tables": ["t_metric"],
                "columns": [
                    "t_metric.group_key",
                    "t_metric.s",
                    "t_metric.v",
                ],
                "result_grain": ["t_metric.group_key"],
                "bindings": [
                    {"logical_name": "分组", "column": "t_metric.group_key"},
                    {"logical_name": "数值", "column": "t_metric.v"},
                    {
                        "logical_name": "筛选列",
                        "column": "t_metric.s",
                        "value_bindings": [
                            {"logical_value": "x", "physical_value": "x"}
                        ],
                    },
                ],
            }
        )
        bound = bind_query_plan(spec, schema)
        sql = (
            'SELECT group_key, COUNT(v) AS "S" FROM t_metric '
            "WHERE s = 'x' GROUP BY group_key ORDER BY s DESC LIMIT 20"
        )
        result = check_plan_conformance(sql, bound, snapshot)
        self.assertTrue(result.accepted, result.as_dict())

    def test_ranking_candidate_cannot_omit_bound_order(self):
        without_order = VALID_SQL.replace(
            "ORDER BY case_count DESC ", ""
        )
        result = check_plan_conformance(without_order, self.bound, SNAPSHOT)
        self.assertFalse(result.accepted)
        self.assertIn("order_by_mismatch", result.errors)

    def test_like_polarity_and_operand_direction_are_exact(self):
        def like_bound(operator):
            spec = QuerySpec.from_dict(
                {
                    "intent": "lookup",
                    "subject": "等级包含强",
                    "dimensions": [
                        {"slot_id": "dimension-level", "concept": "岩爆等级"}
                    ],
                    "filters": [
                        {
                            "slot_id": "filter-level",
                            "field_concept": "岩爆等级",
                            "operator": operator,
                            "value": "%强%",
                        }
                    ],
                    "limit": 20,
                    "expected_shape": "rows",
                }
            )
            plan = SchemaPlan.from_dict(
                {
                    "tables": ["t_detail"],
                    "columns": ["t_detail.c_level"],
                    "result_grain": ["t_detail.c_level"],
                    "bindings": [
                        {
                            "logical_name": "岩爆等级",
                            "column": "t_detail.c_level",
                            "value_bindings": [
                                {
                                    "logical_value": "%强%",
                                    "physical_value": "%强%",
                                }
                            ],
                        }
                    ],
                }
            )
            return bind_query_plan(spec, plan)

        like_plan = like_bound("like")
        not_like_plan = like_bound("not_like")
        positive = (
            "SELECT c_level FROM t_detail "
            "WHERE c_level LIKE '%强%' LIMIT 20"
        )
        negative = (
            "SELECT c_level FROM t_detail "
            "WHERE c_level NOT LIKE '%强%' LIMIT 20"
        )
        self.assertTrue(
            check_plan_conformance(positive, like_plan, SNAPSHOT).accepted
        )
        self.assertTrue(
            check_plan_conformance(negative, not_like_plan, SNAPSHOT).accepted
        )
        self.assertIn(
            "filter_mismatch",
            check_plan_conformance(negative, like_plan, SNAPSHOT).errors,
        )
        self.assertIn(
            "filter_mismatch",
            check_plan_conformance(positive, not_like_plan, SNAPSHOT).errors,
        )
        for sql, plan in (
            (
                "SELECT c_level FROM t_detail "
                "WHERE '%强%' LIKE c_level LIMIT 20",
                like_plan,
            ),
            (
                "SELECT c_level FROM t_detail "
                "WHERE '%强%' NOT LIKE c_level LIMIT 20",
                not_like_plan,
            ),
        ):
            result = check_plan_conformance(sql, plan, SNAPSHOT)
            self.assertFalse(result.accepted)
            self.assertIn("unsupported_filter_expression", result.errors)

    def test_repeated_approved_aggregate_in_order_by_is_not_a_second_measure(self):
        repeated_expression = VALID_SQL.replace(
            "ORDER BY case_count DESC",
            "ORDER BY COUNT(DISTINCT c.c_id) DESC",
        )
        result = check_plan_conformance(repeated_expression, self.bound, SNAPSHOT)
        self.assertTrue(result.accepted, result.as_dict())

    def test_rejects_nondefault_null_ordering_without_a_contract_field(self):
        nondefault = VALID_SQL.replace(
            "ORDER BY case_count DESC",
            "ORDER BY case_count DESC NULLS FIRST",
        )
        result = check_plan_conformance(nondefault, self.bound, SNAPSHOT)
        self.assertFalse(result.accepted)
        self.assertIn("null_ordering_mismatch", result.errors)

    def test_ranking_can_order_by_non_aggregate_measure(self):
        spec = QuerySpec.from_dict(
            {
                "intent": "ranking",
                "subject": "类别排序",
                "dimensions": [
                    {"slot_id": "dimension-id", "concept": "案例ID"}
                ],
                "measures": [
                    {
                        "slot_id": "measure-category",
                        "name": "案例类别",
                        "aggregation": "none",
                        "field_concept": "案例类别",
                    }
                ],
                "order_by": [
                    {
                        "slot_id": "order-category",
                        "target": "measure-category",
                        "direction": "desc",
                    }
                ],
                "limit": 3,
                "expected_shape": "rows",
            }
        )
        plan = SchemaPlan.from_dict(
            {
                "tables": ["t_case"],
                "columns": ["t_case.c_id", "t_case.c_category"],
                "result_grain": ["t_case.c_id"],
                "bindings": [
                    {"logical_name": "案例ID", "column": "t_case.c_id"},
                    {
                        "logical_name": "案例类别",
                        "column": "t_case.c_category",
                    },
                ],
            }
        )
        bound = bind_query_plan(spec, plan)
        result = check_plan_conformance(
            "SELECT c_id, c_category FROM t_case "
            "ORDER BY c_category DESC LIMIT 3",
            bound,
            SNAPSHOT,
        )
        self.assertTrue(result.accepted, result.as_dict())


if __name__ == "__main__":
    unittest.main()
