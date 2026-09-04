"""Text2SQL domain components built on top of EvoAgent's control plane."""

from .knowledge_policy import (
    AuthorityDecision,
    KnowledgeAssertion,
    QueryVersionPin,
    resolve_authority,
)
from .agentic import Text2SQLAgenticEngine
from .benchmark import ResumableEvaluationCheckpoint
from .checkpoint_store import (
    Text2SQLCheckpointBusy,
    Text2SQLCheckpointCorruptionError,
    Text2SQLCheckpointIdentityError,
    Text2SQLRuntimeCheckpointStore,
)
from .contracts import (
    ApprovedQueryPlan,
    BindingConflict,
    BoundQueryPlan,
    JoinSpec,
    PlanBinding,
    QueryDimension,
    QueryFilter,
    QueryMeasure,
    QueryOrder,
    QuerySpec,
    SQLCandidate,
    SQLExecutionResult,
    SQLGateResult,
    SchemaBinding,
    SchemaPlan,
    SchemaValueBinding,
)
from .database_tools import ROLE_TOOL_PERMISSIONS, Text2SQLToolSuite
from .dataset_builder import CATEGORY_TARGETS, build_dataset, generate_cases
from .dataset_review import (
    DatasetReviewStore,
    case_fingerprint,
    verify_review_certificate,
)
from .evaluation import (
    DatasetBundle,
    EvaluationCase,
    Text2SQLEvaluator,
    load_dataset,
    result_fingerprint,
)
from .evolution import Text2SQLEvolutionStore, evaluate_promotion_gate
from .knowledge_store import KnowledgeStore, ROLE_VIEWS
from .markdown_wiki import MarkdownWikiConnector
from .models import Evidence, EvidencePack
from .policy import PolicyArtifact, TEXT2SQL_SKILLS
from .policy_generator import Text2SQLPolicyCandidateGenerator
from .query_plan import (
    PlanConformanceIssue,
    PlanConformanceResult,
    QueryPlanBindingError,
    approve_query_plan,
    bind_query_plan,
    check_candidate_conformance,
    check_plan_conformance,
    plan_conformance,
)
from .schema_catalog import SnapshotArtifacts, build_snapshot_from_dump, write_snapshot_artifacts
from .shadow import Text2SQLShadowReleaseManager, compare_shadow_results
from .sqlite_database import SQLiteBuildResult, build_sqlite_database, open_readonly
from .sql_safety import ReadOnlySQLiteExecutor, validate_sql
from .web_service import Text2SQLWebService

__all__ = [
    "ApprovedQueryPlan",
    "AuthorityDecision",
    "BindingConflict",
    "BoundQueryPlan",
    "CATEGORY_TARGETS",
    "DatasetBundle",
    "DatasetReviewStore",
    "EvaluationCase",
    "JoinSpec",
    "KnowledgeAssertion",
    "KnowledgeStore",
    "MarkdownWikiConnector",
    "QueryVersionPin",
    "QuerySpec",
    "ROLE_TOOL_PERMISSIONS",
    "ReadOnlySQLiteExecutor",
    "ResumableEvaluationCheckpoint",
    "SQLCandidate",
    "SQLExecutionResult",
    "SQLGateResult",
    "SchemaBinding",
    "SchemaPlan",
    "SchemaValueBinding",
    "Evidence",
    "EvidencePack",
    "ROLE_VIEWS",
    "SnapshotArtifacts",
    "SQLiteBuildResult",
    "Text2SQLAgenticEngine",
    "Text2SQLCheckpointBusy",
    "Text2SQLCheckpointCorruptionError",
    "Text2SQLCheckpointIdentityError",
    "Text2SQLEvaluator",
    "Text2SQLEvolutionStore",
    "Text2SQLToolSuite",
    "Text2SQLWebService",
    "Text2SQLPolicyCandidateGenerator",
    "Text2SQLRuntimeCheckpointStore",
    "Text2SQLShadowReleaseManager",
    "PolicyArtifact",
    "PlanBinding",
    "PlanConformanceIssue",
    "PlanConformanceResult",
    "QueryDimension",
    "QueryFilter",
    "QueryMeasure",
    "QueryOrder",
    "QueryPlanBindingError",
    "TEXT2SQL_SKILLS",
    "build_sqlite_database",
    "build_dataset",
    "build_snapshot_from_dump",
    "open_readonly",
    "generate_cases",
    "case_fingerprint",
    "load_dataset",
    "resolve_authority",
    "result_fingerprint",
    "evaluate_promotion_gate",
    "compare_shadow_results",
    "approve_query_plan",
    "bind_query_plan",
    "check_candidate_conformance",
    "check_plan_conformance",
    "plan_conformance",
    "validate_sql",
    "verify_review_certificate",
    "write_snapshot_artifacts",
]
