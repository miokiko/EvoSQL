"""Local Web-facing facade for the Text2SQL Multi-Agent runtime."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from ..config import Settings
from ..llm import JsonChatClient
from .agentic import (
    BUILD_VERSION,
    GATE_IMPLEMENTATION_VERSION,
    TEXT2SQL_PROTOCOL,
    TEXT2SQL_RUNTIME_NODES,
    Text2SQLAgenticEngine,
    build_runtime_identity,
)
from .checkpoint_store import Text2SQLRuntimeCheckpointStore
from .database_tools import ROLE_TOOL_PERMISSIONS
from .evaluation import load_dataset
from .evolution import Text2SQLEvolutionStore
from .memory_attribution import attribute_query_failure
from .memory_service import (
    MemoryWriteStatus,
    extract_user_correction_experiences,
    finalize_run,
    production_experience_source,
)
from .memory_release import (
    REQUIRED_MEMORY_EVALUATION_SPLITS,
    find_matching_baseline,
)
from .policy import TEXT2SQL_SKILLS
from .policy_generator import Text2SQLPolicyCandidateGenerator
from .semantic_rules import generate_semantic_rule, propose_policy_from_rules
from .query_outcome import clarification_continuation, diagnose_result
from .shadow import Text2SQLShadowReleaseManager
from .sql_safety import validate_sql
from .vanna_corpus import (
    DEFAULT_EXCLUDED_TABLES,
    add_confirmed_question_sql,
    build_vanna_corpus,
    question_sql_registry_path,
    remove_confirmed_question_sql,
)
from .vanna_retriever import VannaRetrieverOnly


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_VANNA_PUBLISH_LOCK = threading.RLock()

SKILL_DESCRIPTIONS = {
    "text2sql-lead": "负责查询路由、任务委派、语义计划审批与最终候选选择。",
    "schema-grounding": "将逻辑概念绑定到有证据支持的表、字段、值与 Join。",
    "query-planning": "生成不包含物理表列和 SQL 的逻辑 QuerySpec。",
    "sql-generation": "仅将已批准的查询计划翻译为只读 SQL 候选。",
    "text2sql-critic": "独立盲审 SQL 候选，对语义错误和安全风险执行否决。",
}


_EXPERIENCE_MEMORY_CONTRACT = "ExperienceMemory/v1"
_EXPERIENCE_POLICY_CONTRACT = "ExperiencePolicyProposal/v1"
_EXPERIENCE_STATES = ("candidate", "confirmed", "needs_evidence", "rejected")


def _experience_memory_counts(
    memories: Sequence[Mapping[str, Any]],
) -> Mapping[str, int]:
    """Count only the new Experience contract, excluding legacy runtime hints."""

    counts = {state: 0 for state in _EXPERIENCE_STATES}
    for item in memories:
        rule = item.get("rule")
        if not isinstance(rule, Mapping) or rule.get("contract") != _EXPERIENCE_MEMORY_CONTRACT:
            continue
        state = str(item.get("state") or "")
        if state in counts:
            counts[state] += 1
    return counts


def _policy_records_for_ui(
    evolution: Text2SQLEvolutionStore,
) -> Sequence[Mapping[str, Any]]:
    """Return policy lineage with proposal metadata and optional target replay.

    Newer stores may expose both fields directly.  The metadata fallback keeps
    the Web facade compatible with the current SQLite store during migration.
    """

    records = [dict(item) for item in evolution.list_policies()]
    metadata_by_version: dict[str, Mapping[str, Any]] = {}
    try:
        rows = evolution.connection.execute(
            "SELECT policy_version,proposal_metadata_json FROM policy_versions"
        ).fetchall()
        for row in rows:
            try:
                decoded = json.loads(str(row["proposal_metadata_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                decoded = {}
            if isinstance(decoded, Mapping):
                metadata_by_version[str(row["policy_version"])] = dict(decoded)
    except Exception:
        # An older store without proposal metadata remains readable.
        metadata_by_version = {}

    replay_reader = getattr(evolution, "latest_target_replay", None)
    if not callable(replay_reader):
        replay_reader = getattr(evolution, "get_latest_target_replay", None)

    public_records = []
    for raw in records:
        item = dict(raw)
        version = str(item.get("policy_version") or "")
        metadata = item.get("proposal_metadata")
        if not isinstance(metadata, Mapping):
            metadata = metadata_by_version.get(version, {})
        item["proposal_metadata"] = dict(metadata)
        replay = item.get("target_replay")
        if not isinstance(replay, Mapping) and callable(replay_reader):
            try:
                replay = replay_reader(version)
            except (TypeError, ValueError):
                replay = None
        item["target_replay"] = dict(replay) if isinstance(replay, Mapping) else {}
        public_records.append(item)
    return tuple(public_records)


def _trace_with_compiled_policy_sources(
    evolution: Text2SQLEvolutionStore,
    trace: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Rehydrate non-persisted Policy provenance before feedback extraction."""

    pins = trace.get("version_pins")
    policy_version = str(
        (pins.get("policy_version") if isinstance(pins, Mapping) else "") or ""
    )
    try:
        memory_ids = list(evolution.policy_source_memory_ids(policy_version or None))
    except (TypeError, ValueError):
        memory_ids = []
    return {**dict(trace), "policy_source_memory_ids": memory_ids}


def _production_feedback_eligible(trace: Mapping[str, Any]) -> bool:
    """Only a successful, gate-approved stable user query may teach the system."""

    return bool(
        production_experience_source(
            str(trace.get("origin") or ""),
            str(trace.get("source_lane") or ""),
        )
        and str(trace.get("status") or "") == "success"
        and str(trace.get("query_type") or "DATA_QUERY") == "DATA_QUERY"
        and str(trace.get("final_sql") or "").strip()
        and bool((trace.get("gates") or {}).get("accepted"))
    )


def _feedback_skip_reason(trace: Mapping[str, Any]) -> str:
    if not production_experience_source(
        str(trace.get("origin") or ""),
        str(trace.get("source_lane") or ""),
    ):
        return "non_production_source"
    return "source_run_not_experience_eligible"


def _prompt_fragment_change(
    evolution: Text2SQLEvolutionStore,
    policy: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Project the bounded same-Agent Prompt change without exposing artifacts."""

    target_agent = str(policy.get("target_skill") or "")
    version = str(policy.get("policy_version") or "")
    parent_version = str(policy.get("parent_version") or "")
    if target_agent not in TEXT2SQL_SKILLS or not version or not parent_version:
        return {}
    try:
        before = str(
            evolution.get_policy(parent_version)
            .role_policy(target_agent)
            .get("prompt_fragment")
            or ""
        )[:4000]
        after = str(
            evolution.get_policy(version)
            .role_policy(target_agent)
            .get("prompt_fragment")
            or ""
        )[:4000]
    except (TypeError, ValueError):
        return {}
    return {
        "target_agent": target_agent,
        "before": before,
        "after": after,
        "changed": before != after,
    }


def _project_path(environment_name: str, default: str) -> Path:
    value = Path(os.getenv(environment_name, default))
    return value.resolve() if value.is_absolute() else (PROJECT_ROOT / value).resolve()


def _public_gate_results(value: Any) -> list[Mapping[str, Any]]:
    """Return bounded deterministic gate evidence without candidate SQL bodies."""

    results = []
    for raw in value or ():
        if not isinstance(raw, Mapping) or len(results) >= 4:
            continue
        validation = dict(raw.get("validation") or {})
        validation.pop("normalized_sql", None)
        conformance = dict(raw.get("plan_conformance") or {})
        conformance_sql_gate = dict(conformance.get("sql_gate") or {})
        if conformance_sql_gate:
            conformance_sql_gate.pop("normalized_sql", None)
            conformance["sql_gate"] = conformance_sql_gate
        results.append(
            {
                "candidate_index": raw.get("candidate_index"),
                "candidate_id": str(raw.get("candidate_id") or "")[:100],
                "accepted": bool(raw.get("accepted")),
                "validation": validation,
                "plan_conformance": conformance,
                "explain": dict(raw.get("explain") or {}),
                "errors": [
                    str(item)[:200] for item in (raw.get("errors") or ())[:20]
                ],
                "runtime_error": str(raw.get("runtime_error") or "")[:500],
            }
        )
    return results


def _public_plan_payload(collaboration: Mapping[str, Any]) -> Mapping[str, Any]:
    """Select the plan-first protocol state that is safe and useful in the UI."""

    generation = dict(collaboration.get("sql_generation_result") or {})
    generation_output = dict(generation.get("output") or {})
    gate_results = _public_gate_results(collaboration.get("candidate_gate_results"))
    rounds = []
    for raw in collaboration.get("candidate_gate_rounds") or ():
        if not isinstance(raw, Mapping) or len(rounds) >= 2:
            continue
        round_results = _public_gate_results(raw.get("candidate_gate_results"))
        rounds.append(
            {
                "round": int(raw.get("round") or 0),
                "accepted_candidate_count": len(raw.get("accepted_candidates") or ()),
                "candidate_gate_results": round_results,
                "gate_issues": [
                    {
                        "candidate_id": str(item.get("candidate_id") or "")[:100],
                        "code": str(item.get("code") or "")[:200],
                        "message": str(item.get("message") or "")[:500],
                    }
                    for item in (raw.get("gate_issues") or ())[:20]
                    if isinstance(item, Mapping)
                ],
            }
        )
    if not gate_results and rounds:
        gate_results = list(rounds[-1]["candidate_gate_results"])
    return {
        "bound_query_plan": dict(collaboration.get("bound_query_plan") or {}),
        "approved_query_plan": dict(
            collaboration.get("approved_query_plan") or {}
        ),
        "binding_conflicts": [
            dict(item)
            for item in (collaboration.get("binding_conflicts") or ())[:50]
            if isinstance(item, Mapping)
        ],
        "sql_generation": {
            "role": "sql-generation",
            "status": str(generation.get("status") or "not-run")[:50],
            "candidate_count": len(generation_output.get("sql_candidates") or ()),
            "generation_notes": [
                str(item)[:500]
                for item in (generation_output.get("generation_notes") or ())[:20]
            ],
            "repair_count": int(collaboration.get("sql_generation_repairs") or 0),
            "error": str(generation.get("error") or "")[:500],
        },
        "candidate_gate_results": gate_results,
        "candidate_gate_rounds": rounds,
        "sql_generation_repairs": int(
            collaboration.get("sql_generation_repairs") or 0
        ),
    }


def _public_runtime_payload(
    collaboration: Mapping[str, Any], gates: Mapping[str, Any]
) -> Mapping[str, Any]:
    plan = _public_plan_payload(collaboration)
    return {
        "role": "text2sql-harness",
        "classification": "deterministic-runtime",
        "is_skill": False,
        "protocol": TEXT2SQL_PROTOCOL,
        "node_count": len(TEXT2SQL_RUNTIME_NODES),
        "nodes": list(TEXT2SQL_RUNTIME_NODES),
        "build_version": BUILD_VERSION,
        "gate_implementation_version": GATE_IMPLEMENTATION_VERSION,
        "binding": {
            "accepted": bool(plan["bound_query_plan"])
            and not bool(plan["binding_conflicts"]),
            "conflict_count": len(plan["binding_conflicts"]),
        },
        "candidate_gates": {
            "round_count": len(plan["candidate_gate_rounds"]),
            "accepted_count": sum(
                bool(item.get("accepted"))
                for item in plan["candidate_gate_results"]
            ),
            "repair_count": plan["sql_generation_repairs"],
        },
        "final_gates": dict(gates),
    }


class Text2SQLWebService:
    """Expose a bounded Text2SQL query without leaking internal model prompts."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: Optional[JsonChatClient] = None,
        llm_config: Optional[Mapping[str, Any]] = None,
        database_path: Optional[Path] = None,
        snapshot_path: Optional[Path] = None,
        vanna_index_root: Optional[Path] = None,
        business_knowledge_root: Optional[Path] = None,
        join_catalog_path: Optional[Path] = None,
        evolution_store_path: Optional[Path] = None,
        checkpoint_store_path: Optional[Path] = None,
        dataset_path: Optional[Path] = None,
    ) -> None:
        self.settings = settings
        self.llm_config = dict(llm_config if llm_config is not None else settings.resolved_llm())
        self.client = client
        if self.client is None and self.llm_config:
            self.client = JsonChatClient(
                str(self.llm_config["base_url"]),
                str(self.llm_config["api_key"]),
                str(self.llm_config["model"]),
                provider=str(self.llm_config["provider"]),
                timeout=settings.agent_time_budget_seconds,
                extra_headers=dict(self.llm_config.get("headers") or {}),
            )
        self.database_path = database_path or _project_path(
            "EVOAGENT_TEXT2SQL_SQLITE_PATH", "database/evo_text2sql_eval.sqlite3"
        )
        self.snapshot_path = snapshot_path or _project_path(
            "EVOAGENT_TEXT2SQL_SCHEMA_SNAPSHOT",
            "artifacts/text2sql/schema/database_snapshot.json",
        )
        self.vanna_index_root = vanna_index_root or _project_path(
            "EVOAGENT_TEXT2SQL_VANNA_ROOT",
            "artifacts/text2sql/vanna",
        )
        self.business_knowledge_root = business_knowledge_root or _project_path(
            "EVOAGENT_TEXT2SQL_BUSINESS_ROOT",
            "knowledge/business",
        )
        self.join_catalog_path = join_catalog_path or _project_path(
            "EVOAGENT_TEXT2SQL_JOIN_CATALOG",
            "artifacts/text2sql/schema/join_catalog.review.json",
        )
        self.evolution_store_path = evolution_store_path or _project_path(
            "EVOAGENT_TEXT2SQL_EVOLUTION_STORE",
            "artifacts/text2sql/evolution/evolution.sqlite3",
        )
        self.checkpoint_store_path = checkpoint_store_path or _project_path(
            "EVOAGENT_TEXT2SQL_CHECKPOINT_STORE",
            "artifacts/text2sql/checkpoints/runtime.sqlite3",
        )
        self.dataset_path = dataset_path or _project_path(
            "EVOAGENT_TEXT2SQL_DATASET", "evaluation/datasets/text2sql_v1"
        )

    def _snapshot(self) -> Mapping[str, Any]:
        return json.loads(self.snapshot_path.read_text(encoding="utf-8"))

    @property
    def model_ready(self) -> bool:
        return self.client is not None and bool(self.llm_config)

    def _join_catalog(self) -> Optional[Mapping[str, Any]]:
        if not self.join_catalog_path.exists():
            return None
        value = json.loads(self.join_catalog_path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError("Join Catalog must be a JSON object")
        return value

    def _vanna_version(self) -> str:
        return VannaRetrieverOnly.current_index_version(self.vanna_index_root)

    def _vanna_status(self) -> Mapping[str, Any]:
        version = self._vanna_version()
        if not version:
            return {
                "enabled": True,
                "ready": False,
                "mode": "retriever_only",
                "generation_enabled": False,
                "sql_execution_enabled": False,
                "node2_draft_mode": "frozen_retrieval_untrusted_no_execute",
                "node2_draft_generation_enabled": True,
                "node2_draft_sql_execution_enabled": False,
                "index_version": "",
                "counts": {},
                "corpus_counts": {},
                "database_snapshot_id": "",
            }
        return {
            **dict(VannaRetrieverOnly(self.vanna_index_root, version).status()),
            "node2_draft_mode": "frozen_retrieval_untrusted_no_execute",
            "node2_draft_generation_enabled": True,
            "node2_draft_sql_execution_enabled": False,
        }

    def _build_vanna(self, snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
        return build_vanna_corpus(
            self.vanna_index_root,
            snapshot,
            business_root=self.business_knowledge_root,
            join_catalog=self._join_catalog(),
            question_sql_path=question_sql_registry_path(self.vanna_index_root),
            enabled=True,
        )

    def _runtime_vanna_pin(
        self, snapshot: Mapping[str, Any]
    ) -> tuple[str, bool]:
        status = dict(self._vanna_status())
        version = str(status.get("index_version") or "")
        if (
            version
            and status.get("ready")
            and status.get("database_snapshot_id") == snapshot["snapshot_id"]
        ):
            return version, True
        raise RuntimeError(
            "Vanna 语料尚未构建或与当前 Schema Snapshot 不一致；"
            "请先运行 scripts/build_text2sql_vanna.py"
        )

    def _query_attempt_runtime_identity(
        self,
        version_pins: Mapping[str, str],
        conversation_context: Mapping[str, Any],
        policy_source_memory_ids: Sequence[str] = (),
    ) -> Mapping[str, Any]:
        """Bind Web idempotency cache entries to the complete runtime release."""

        canonical_context = json.dumps(
            conversation_context,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        runtime = build_runtime_identity(
            token_budget=self.settings.agent_token_budget,
            time_budget=self.settings.agent_time_budget_seconds,
            max_rows=200,
            timeout_ms=3000,
            policy_source_memory_ids=policy_source_memory_ids,
        )
        return {
            "version_pins": dict(version_pins),
            "conversation_context_sha256": hashlib.sha256(
                canonical_context.encode("utf-8")
            ).hexdigest(),
            "model": {
                "provider": str(getattr(self.client, "provider", "unknown")),
                "model": str(getattr(self.client, "model", "unknown")),
                "temperature": 0,
            },
            **dict(runtime),
        }

    def status(self) -> Mapping[str, Any]:
        snapshot = self._snapshot()
        dataset_status: dict[str, Any]
        try:
            # The local console accepts the repository-owned reviewed dataset
            # after validating every file hash, case attestation and checklist.
            # Release/evaluation entry points keep load_dataset's strict default
            # and still require the human-held HMAC key.
            dataset = load_dataset(
                self.dataset_path, require_review_signature=False
            )
            dataset_status = {
                "dataset_id": dataset.dataset_id,
                "dataset_sha256": dataset.dataset_sha256,
                "case_count": sum(dataset.split_counts.values()),
                "split_counts": dict(dataset.split_counts),
                "review_verified": bool(dataset.review_evidence.get("verified")),
                "review_signature_verified": bool(
                    dataset.review_evidence.get("signature_verified")
                ),
                "review_verification_mode": str(
                    dataset.review_evidence.get("verification_mode") or ""
                ),
                "reviewed_case_count": int(
                    dataset.review_evidence.get("reviewed_case_count") or 0
                ),
                "certificate_sha256": str(
                    dataset.review_evidence.get("certificate_sha256") or ""
                ),
            }
        except Exception as exc:
            dataset_status = {"review_verified": False, "error": str(exc)[:500]}
        vanna_status = dict(self._vanna_status())
        corpus_counts = dict(vanna_status.get("corpus_counts") or {})
        source_counts = dict(vanna_status.get("source_counts") or {})
        knowledge_sources = {
            "schema": {
                "count": int(corpus_counts.get("schema") or 0),
                "table_count": int((vanna_status.get("counts") or {}).get("ddl") or 0),
                "value_count": int(corpus_counts.get("value") or 0),
            },
            "business_documents": {
                "count": int(source_counts.get("business_document") or 0),
                "root": "knowledge/business",
            },
            "question_sql": {
                "count": int(source_counts.get("user_confirmed") or 0),
            },
            "approved_joins": {
                "count": int(source_counts.get("join_catalog") or 0),
            },
            "excluded_tables": sorted(DEFAULT_EXCLUDED_TABLES),
        }
        with Text2SQLEvolutionStore(self.evolution_store_path, snapshot) as evolution:
            release = Text2SQLShadowReleaseManager(evolution).status()
            experience_counts = {
                state: len(evolution.list_experiences(state, 100))
                for state in ("candidate", "ineligible", "promoted", "rejected")
            }
            semantic_memories = list(evolution.list_memory())
            semantic_experience_counts = dict(
                _experience_memory_counts(semantic_memories)
            )
            policy_records = _policy_records_for_ui(evolution)
            policy_candidates = [
                {
                    **dict(item),
                    "prompt_fragment_change": dict(
                        _prompt_fragment_change(evolution, item)
                    ),
                }
                for item in policy_records
                if (
                    (item.get("proposal_metadata") or {}).get("contract")
                    == _EXPERIENCE_POLICY_CONTRACT
                    or (item.get("proposal_metadata") or {}).get("source")
                    == "confirmed-experiences"
                )
            ][-12:]
            evolution_status = {
                "active_policy_version": evolution.active_policy_version,
                "memory_snapshot_id": evolution.memory_snapshot_id,
                "stable_memory_count": len(evolution.list_memory("stable")),
                "candidate_memory_count": len(evolution.list_memory("candidate")),
                "experience_counts": experience_counts,
                "semantic_experience_counts": semantic_experience_counts,
                "policy_candidates": policy_candidates,
                "release": release,
            }
        return {
            "ready": self.model_ready
            and self.database_path.exists()
            and bool(dataset_status.get("review_verified"))
            and bool(vanna_status.get("ready")),
            "model": {
                "configured": self.model_ready,
                "requested_provider": self.settings.llm_provider,
                "provider": self.llm_config.get("provider", ""),
                "model": self.llm_config.get("model", self.settings.llm_model),
                "local_ollama_used": False,
            },
            "database": {
                "ready": self.database_path.exists(),
                "snapshot_id": snapshot["snapshot_id"],
                "table_count": len(snapshot.get("tables") or ()),
                "readonly": True,
            },
            "dataset": dataset_status,
            "knowledge_sources": knowledge_sources,
            "vanna": vanna_status,
            "evolution": evolution_status,
            "roles": list(TEXT2SQL_SKILLS),
            "deterministic_runtime": {
                "role": "text2sql-harness",
                "classification": "deterministic-runtime",
                "is_skill": False,
                "protocol": TEXT2SQL_PROTOCOL,
                "node_count": len(TEXT2SQL_RUNTIME_NODES),
                "nodes": list(TEXT2SQL_RUNTIME_NODES),
                "build_version": BUILD_VERSION,
                "gate_implementation_version": GATE_IMPLEMENTATION_VERSION,
            },
        }

    def skills(self) -> Mapping[str, Any]:
        """Return the active, role-scoped Text2SQL Skill catalog."""
        snapshot = self._snapshot()
        with Text2SQLEvolutionStore(self.evolution_store_path, snapshot) as evolution:
            policy = evolution.get_policy()
            policies = evolution.list_policies()
            skills = []
            for name in TEXT2SQL_SKILLS:
                default_tools = ROLE_TOOL_PERMISSIONS[name]
                config = policy.role_policy(name)
                configured_tools = config.get("allowed_tools")
                skills.append(
                    {
                        "name": name,
                        "description": SKILL_DESCRIPTIONS.get(name, "Text2SQL runtime skill"),
                        "policy_version": policy.version,
                        "prompt_fragment": str(config.get("prompt_fragment") or ""),
                        "allowed_tools": list(
                            configured_tools
                            if configured_tools is not None
                            else default_tools
                        ),
                        "field_alias_count": len(config.get("field_aliases") or {}),
                        "value_alias_count": len(config.get("value_aliases") or {}),
                        "few_shot_count": len(config.get("few_shot_examples") or ()),
                        "budget_parameters": dict(config.get("budget_parameters") or {}),
                    }
                )
            candidates = [
                dict(item)
                for item in policies
                if item.get("status") not in {"approved", "retired"}
            ]
            return {
                "active_policy_version": evolution.active_policy_version,
                "skills": skills,
                "candidate_count": len(candidates),
                "candidates": candidates[-12:],
                "submission_contract": "text2sql-role-skill-v2",
            }

    def propose_skill(
        self,
        skill_name: str,
        patch: Mapping[str, Any],
        change_reason: str,
        created_by: str,
    ) -> Mapping[str, Any]:
        """Save one bounded Text2SQL Skill change as an isolated candidate."""
        skill_name = skill_name.strip()
        if skill_name not in TEXT2SQL_SKILLS:
            raise ValueError("unsupported Text2SQL skill")
        if not isinstance(patch, Mapping):
            raise ValueError("skill patch must be an object")
        allowed = {
            "prompt_fragment",
            "field_aliases",
            "value_aliases",
            "few_shot_examples",
            "allowed_tools",
            "budget_parameters",
        }
        unknown = set(patch).difference(allowed)
        if unknown:
            raise ValueError(
                "skill patch contains unsupported fields: %s"
                % ", ".join(sorted(unknown))
            )
        if not patch:
            raise ValueError("skill patch must change at least one field")
        snapshot = self._snapshot()
        with Text2SQLEvolutionStore(self.evolution_store_path, snapshot) as evolution:
            artifact = evolution.get_policy().as_dict()
            field_mapping = {
                "prompt_fragment": "prompt_fragments",
                "field_aliases": "field_aliases",
                "value_aliases": "value_aliases",
                "few_shot_examples": "few_shot_examples",
                "allowed_tools": "tool_selection_policy",
                "budget_parameters": "budget_parameters",
            }
            for patch_field, value in patch.items():
                artifact[field_mapping[patch_field]][skill_name] = value
            version = evolution.propose_policy(
                artifact,
                skill_name,
                change_reason,
                created_by,
                proposal_metadata={
                    "source": "text2sql-web-skill-submission",
                    "contract": "text2sql-role-skill-v2",
                },
            )
            return {
                "skill_name": skill_name,
                "candidate_policy_version": version,
                "parent_policy_version": evolution.active_policy_version,
                "status": "candidate",
                "next_step": "run_validation_and_sealed_holdout",
            }

    def propose_policy_from_experiences(
        self,
        memory_ids: Sequence[str],
        created_by: str,
        change_reason: str = "",
    ) -> Mapping[str, Any]:
        """Compile explicitly selected Confirmed Experiences into one candidate."""

        if not self.model_ready or self.client is None:
            raise RuntimeError("configured LLM is required for Policy generation")
        normalized_ids = tuple(
            str(value).strip()[:200] for value in memory_ids if str(value).strip()
        )
        if not normalized_ids:
            raise ValueError("at least one confirmed Experience is required")
        if len(normalized_ids) > 50:
            raise ValueError("at most 50 Experiences may generate one Policy")
        if len(set(normalized_ids)) != len(normalized_ids):
            raise ValueError("selected Experience ids must not contain duplicates")
        snapshot = self._snapshot()
        with Text2SQLEvolutionStore(self.evolution_store_path, snapshot) as evolution:
            experiences = [evolution.get_memory(memory_id) for memory_id in normalized_ids]
            parent = evolution.get_policy()
            generated = Text2SQLPolicyCandidateGenerator(
                self.client, self.settings.agent_token_budget
            ).generate_from_confirmed_experiences(
                experiences,
                parent,
                snapshot,
            )
            target_agent = str(generated["target_agent"])
            reason = (
                change_reason.strip()
                or str(generated.get("rationale") or "").strip()
                or "Confirmed Experience Policy proposal"
            )
            metadata = {
                key: value
                for key, value in generated.items()
                if key not in {"artifact", "policy_version"}
            }
            metadata.update(
                {
                    "source": "confirmed-experiences",
                    "contract": "ExperiencePolicyProposal/v1",
                    "target_replay_required": True,
                }
            )
            version = evolution.propose_policy(
                generated["artifact"],
                target_agent,
                reason,
                created_by,
                parent.version,
                metadata,
            )
            return {
                "candidate_policy_version": version,
                "parent_policy_version": parent.version,
                "target_agent": target_agent,
                "memory_ids": list(generated["memory_ids"]),
                "clusters": list(generated["clusters"]),
                "rationale": str(generated["rationale"]),
                "generation": dict(generated["generation"]),
                "status": "candidate",
                "target_replay_status": "pending",
                "next_step": "run_target_replay",
            }

    def generate_semantic_rule(self, memory_id: str, actor: str) -> Mapping[str, Any]:
        if not self.model_ready or self.client is None:
            raise RuntimeError("configured LLM is required for SemanticRule generation")
        with Text2SQLEvolutionStore(self.evolution_store_path, self._snapshot()) as evolution:
            return generate_semantic_rule(evolution, self.client, memory_id, actor,
                                          self.settings.agent_token_budget)

    def review_semantic_rule(self, rule_id: str, decision: str, actor: str,
                             note: str = "") -> Mapping[str, Any]:
        with Text2SQLEvolutionStore(self.evolution_store_path, self._snapshot()) as evolution:
            return evolution.review_semantic_rule(rule_id, decision, actor, note)

    def propose_policy_from_rules(self, rule_ids: Sequence[str], actor: str,
                                  reason: str = "") -> Mapping[str, Any]:
        if not self.model_ready or self.client is None:
            raise RuntimeError("configured LLM is required for Policy generation")
        with Text2SQLEvolutionStore(self.evolution_store_path, self._snapshot()) as evolution:
            return propose_policy_from_rules(evolution, self.client, rule_ids, actor,
                                              reason, self.settings.agent_token_budget)

    def traces(self, limit: int = 20) -> Mapping[str, Any]:
        """Return bounded, public Text2SQL traces from the local control database."""
        bounded = max(1, min(int(limit), 50))
        snapshot = self._snapshot()
        with Text2SQLEvolutionStore(self.evolution_store_path, snapshot) as evolution:
            values = []
            for stored in evolution.list_query_traces(bounded):
                item = dict(stored)
                collaboration = dict(item.get("collaboration") or {})
                item.update(_public_plan_payload(collaboration))
                item["clarification"] = dict(collaboration.get("clarification") or {})
                item["diagnostic"] = dict(collaboration.get("diagnostic") or diagnose_result(item))
                item["deterministic_runtime"] = _public_runtime_payload(
                    collaboration, dict(item.get("gates") or {})
                )
                for private_key in (
                    "result_rows",
                    "collaboration",
                    "user_id",
                    "session_id",
                ):
                    item.pop(private_key, None)
                values.append(item)
        return {"traces": values, "retention": "local-sqlite", "limit": bounded}

    def memory(
        self, user_id: str, session_id: str, limit: int = 12
    ) -> Mapping[str, Any]:
        """Return four memory layers, falling back to the latest visible session."""
        snapshot = self._snapshot()
        requested_session_id = session_id[:200]
        display_session_id = requested_session_id
        with Text2SQLEvolutionStore(self.evolution_store_path, snapshot) as evolution:
            dashboard = dict(
                evolution.memory_dashboard(user_id, requested_session_id, limit)
            )
            sessions = list(evolution.list_memory_sessions(user_id))
            current_session_empty = (
                int(dashboard["working"]["count"]) == 0
                and int(dashboard["episodic"]["count"]) == 0
            )
            if current_session_empty:
                fallback = next(
                    (
                        item
                        for item in sessions
                        if item["session_id"] != requested_session_id
                    ),
                    None,
                )
                if fallback:
                    display_session_id = str(fallback["session_id"])
                    dashboard = dict(
                        evolution.memory_dashboard(
                            user_id, display_session_id, limit
                        )
                    )
            all_semantic_memories = list(evolution.list_memory())
            semantic_experience_counts = dict(
                _experience_memory_counts(all_semantic_memories)
            )
            policy_usage: dict[str, list[str]] = {}
            for policy in _policy_records_for_ui(evolution):
                metadata = policy.get("proposal_metadata") or {}
                if not isinstance(metadata, Mapping):
                    continue
                memory_ids = metadata.get("compiled_memory_ids") or metadata.get(
                    "memory_ids"
                )
                if not isinstance(memory_ids, Sequence) or isinstance(
                    memory_ids, (str, bytes)
                ):
                    continue
                version = str(policy.get("policy_version") or "")
                for memory_id in memory_ids:
                    key = str(memory_id)
                    if key and version:
                        policy_usage.setdefault(key, []).append(version)
            semantic_layer = dict(dashboard["semantic"])
            semantic_layer["experience_counts"] = semantic_experience_counts
            semantic_layer["items"] = [
                {
                    **dict(item),
                    "used_in_policy_versions": list(
                        dict.fromkeys(policy_usage.get(str(item.get("memory_id") or ""), ()))
                    ),
                }
                for item in semantic_layer.get("items") or ()
            ]
            snapshot_id = evolution.memory_snapshot_id
            semantic_rules = list(evolution.list_semantic_rules(limit=50))
            semantic_rule_counts = dict(evolution.semantic_rule_counts())
        return {
            "contract": "Text2SQLMemoryDashboard/v1",
            "semantic_rules": {
                "items": semantic_rules,
                "counts": semantic_rule_counts,
                "target_agents": list(TEXT2SQL_SKILLS),
                "role_scoped": True,
                "direct_runtime_injection": False,
            },
            "memory_snapshot_id": snapshot_id,
            "storage": "local-sqlite",
            "session_id": requested_session_id,
            "session_view": {
                "requested_session_id": requested_session_id,
                "display_session_id": display_session_id,
                "mode": (
                    "latest_history"
                    if display_session_id != requested_session_id
                    else "current"
                ),
                "current_session_empty": current_session_empty,
                "available_sessions": sessions,
            },
            "layers": {
                "working": dashboard["working"],
                "episodic": dashboard["episodic"],
                "semantic": semantic_layer,
            },
            "question_sql": dashboard["question_sql"],
            "decision_contract": {
                "harness": "execution_gate",
                "human": "result_review",
                "sources_are_independent": True,
                "human_rejection_requires_reason": True,
            },
            "evaluations": {
                "items": [
                    {
                        key: item[key]
                        for key in (
                            "job_id",
                            "memory_id",
                            "status",
                            "phase",
                            "progress_current",
                            "progress_total",
                            "error",
                            "requested_by",
                            "created_at",
                            "updated_at",
                        )
                    }
                    for item in dashboard["memory_evaluations"]["items"]
                ]
            },
            "boundaries": {
                "raw_model_reasoning_exposed": False,
                "result_rows_exposed": False,
                "stable_semantic_memory_only_injected": True,
                "experience_direct_runtime_injection": False,
                "vanna_is_separate_knowledge_domain": True,
            },
        }

    def experiences(self, state: str = "", limit: int = 50) -> Mapping[str, Any]:
        snapshot = self._snapshot()
        with Text2SQLEvolutionStore(self.evolution_store_path, snapshot) as evolution:
            values = list(evolution.list_experiences(state, limit))
            confirmable_ids = set()
            for value in values:
                if (
                    str(value.get("state") or "") != "ineligible"
                    or "requires_human_feedback"
                    not in set(value.get("eligibility_reasons") or ())
                ):
                    continue
                try:
                    trace = evolution.get_query_trace(str(value.get("task_id") or ""))
                except ValueError:
                    continue
                if _production_feedback_eligible(trace):
                    confirmable_ids.add(str(value.get("experience_id") or ""))
        public_values = []
        for value in values:
            item = dict(value)
            item["confirmable"] = str(item.get("experience_id") or "") in confirmable_ids
            public_values.append(item)
        return {"experiences": public_values, "state": state or "all"}

    def confirm_experience(
        self, experience_id: str, actor: str, note: str = ""
    ) -> Mapping[str, Any]:
        """Publish a human-confirmed QueryRun as a Vanna Question-SQL case.

        Question-SQL cases are separate from role-scoped Agent Semantic Memory.
        The local records provide provenance and revocation metadata; Vanna is
        the retrieval backend that receives the reusable pair.
        """

        snapshot = self._snapshot()
        with Text2SQLEvolutionStore(self.evolution_store_path, snapshot) as evolution:
            item = evolution.get_experience(experience_id)
            already_confirmed = item["state"] == "promoted"
            awaiting_feedback = (
                item["state"] == "ineligible"
                and "requires_human_feedback"
                in set(item.get("eligibility_reasons") or ())
            )
            confirmed_candidate = (
                item["state"] == "candidate"
                and item["eligible"]
                and item["user_feedback"] == "correct"
            )
            if not already_confirmed and not awaiting_feedback and not confirmed_candidate:
                raise ValueError("experience is not awaiting human confirmation")
            trace = _trace_with_compiled_policy_sources(
                evolution,
                evolution.get_query_trace(str(item.get("task_id") or "")),
            )
            if not _production_feedback_eligible(trace):
                raise ValueError(
                    "only successful gate-approved stable Web/CLI QueryRuns "
                    "can become reusable experiences"
                )
            gate = validate_sql(str(item["sql"]), snapshot)
            if not gate.accepted:
                raise ValueError(
                    "experience SQL failed the deterministic gate: %s"
                    % ", ".join(gate.errors)
                )
            if already_confirmed:
                confirmed_id = experience_id
                confirmed = item
            else:
                confirmed_id = evolution.add_experience_candidate(
                    str(item["task_id"]),
                    str(item["question"]),
                    str(item["sql"]),
                    source_kind="human_confirmed_query",
                    eligible=True,
                )
                if awaiting_feedback:
                    evolution.record_query_feedback(
                        str(item["task_id"]), "correct", note, actor
                    )
                confirmed = evolution.get_experience(confirmed_id)
        gate = validate_sql(str(confirmed["sql"]), snapshot)
        with _VANNA_PUBLISH_LOCK:
            question_sql = add_confirmed_question_sql(
                question_sql_registry_path(self.vanna_index_root),
                database_snapshot_id=str(snapshot["snapshot_id"]),
                question=str(confirmed["question"]),
                sql=str(confirmed["sql"]),
                actor=actor,
                source_id=confirmed_id,
                dependencies=tuple([*gate.tables, *gate.columns]),
            )
            vanna = self._build_vanna(snapshot)

        with Text2SQLEvolutionStore(self.evolution_store_path, snapshot) as evolution:
            promoted = evolution.promote_confirmed_experience(
                confirmed_id,
                str(question_sql["evidence_id"]),
                actor,
                note,
            )
        return {
            **dict(promoted),
            "confirmation": (
                "already-confirmed" if already_confirmed else "human-confirmed"
            ),
            "question_sql": dict(question_sql),
            "vanna": dict(vanna),
            "memory_kind": "vanna_question_sql",
            "semantic_memory_written": False,
            "next_step": "available_in_vanna",
        }

    def revoke_confirmed_experience(
        self, experience_id: str, actor: str, reason: str
    ) -> Mapping[str, Any]:
        """Remove a mistaken Q-SQL from Vanna without deleting its audit trail."""

        if not reason.strip():
            raise ValueError("revoking Question-SQL requires a reason")
        snapshot = self._snapshot()
        with Text2SQLEvolutionStore(self.evolution_store_path, snapshot) as evolution:
            item = evolution.get_experience(experience_id)
            if item["state"] != "promoted":
                raise ValueError("only a promoted Question-SQL can be revoked")
        with _VANNA_PUBLISH_LOCK:
            removal = remove_confirmed_question_sql(
                question_sql_registry_path(self.vanna_index_root),
                evidence_id=str(item.get("knowledge_evidence_id") or ""),
                source_id=experience_id,
            )
            if not removal["removed"]:
                raise ValueError("confirmed Question-SQL is missing from the registry")
            vanna = self._build_vanna(snapshot)
        with Text2SQLEvolutionStore(self.evolution_store_path, snapshot) as evolution:
            revoked = evolution.revoke_confirmed_experience(
                experience_id, actor, reason
            )
        return {
            **dict(revoked),
            "question_sql": dict(removal),
            "vanna": dict(vanna),
            "next_step": "removed_from_vanna",
        }

    def feedback_experience(
        self,
        experience_id: str,
        decision: str,
        note: str,
        corrected_sql: str,
        actor: str,
    ) -> Mapping[str, Any]:
        """Record review-surface feedback without relying on browser session state."""

        if decision not in {"correct", "incorrect"}:
            raise ValueError("feedback decision must be correct or incorrect")
        note = note.strip()
        corrected_sql = corrected_sql.strip()
        if decision == "incorrect" and not note:
            raise ValueError("incorrect feedback requires a reason")

        snapshot = self._snapshot()
        with Text2SQLEvolutionStore(self.evolution_store_path, snapshot) as evolution:
            item = evolution.get_experience(experience_id)
            trace = _trace_with_compiled_policy_sources(
                evolution,
                evolution.get_query_trace(str(item.get("task_id") or "")),
            )
            is_production_source = _production_feedback_eligible(trace)
            awaiting_feedback = (
                item["state"] == "ineligible"
                and "requires_human_feedback"
                in set(item.get("eligibility_reasons") or ())
            )
            if not is_production_source:
                if not awaiting_feedback:
                    raise ValueError("experience is not awaiting human feedback")
                human_decision = evolution.record_query_feedback(
                    str(item["task_id"]), decision, note, actor
                )
                recorded = evolution.get_experience(experience_id)
                return {
                    **dict(recorded),
                    "feedback": decision,
                    "decision": dict(human_decision),
                    "memory_id": "",
                    "corrected_experience_id": "",
                    "attribution": {},
                    "experience_skipped_reason": _feedback_skip_reason(trace),
                    "source_origin": str(trace.get("origin") or ""),
                    "source_lane": str(trace.get("source_lane") or ""),
                    "next_step": "feedback_recorded",
                }

            if decision == "incorrect":
                if not awaiting_feedback:
                    raise ValueError("experience is not awaiting human feedback")
                if not str(trace.get("final_sql") or "").strip():
                    raise ValueError("experience QueryRun has no SQL to review")

                if corrected_sql:
                    if corrected_sql == str(item.get("sql") or "").strip():
                        raise ValueError(
                            "corrected SQL must differ from the rejected SQL"
                        )
                    corrected_gate = validate_sql(corrected_sql, snapshot)
                    if not corrected_gate.accepted:
                        raise ValueError(
                            "corrected SQL failed the deterministic gate: %s"
                            % ", ".join(corrected_gate.errors)
                        )

                attribution = attribute_query_failure(
                    trace,
                    snapshot,
                    corrected_sql=corrected_sql,
                    feedback_note=note,
                )
                failure_kind = str(attribution.get("failure_kind") or "")
                confident_owner = bool(
                    corrected_sql
                    or failure_kind != "critic_false_accept"
                    or "critic" in note.casefold()
                    or "评审" in note
                )
                feedback_evidence: dict[str, Any] = {
                    "decision": "incorrect",
                    "note": note,
                    "corrected_sql": corrected_sql,
                }
                if confident_owner:
                    feedback_evidence.update(
                        {
                            "target_agent": str(
                                attribution.get("target_skill") or ""
                            ),
                            "problem_code": failure_kind,
                            "correction": str(
                                (attribution.get("rule") or {}).get("action")
                                or attribution.get("content")
                                or ""
                            ),
                        }
                    )
                extracted = extract_user_correction_experiences(
                    trace,
                    feedback_evidence,
                    snapshot=snapshot,
                )
                if not extracted:
                    raise ValueError(
                        "feedback did not produce a reviewable Experience"
                    )
                memory_id = evolution.add_experience_memory(
                    extracted[0], origin_split="production_feedback"
                )
                evolution.record_query_feedback(
                    str(item["task_id"]), "incorrect", note, actor
                )
                corrected_experience_id = ""
                if corrected_sql:
                    corrected_experience_id = evolution.add_experience_candidate(
                        str(item["task_id"]),
                        str(item["question"]),
                        corrected_sql,
                        source_kind="human_corrected_sql",
                        eligible=True,
                    )
                rejected = evolution.get_experience(experience_id)

        if decision == "correct":
            return self.confirm_experience(experience_id, actor, note)

        return {
            **dict(rejected),
            "feedback": "incorrect",
            "memory_id": memory_id,
            "corrected_experience_id": corrected_experience_id,
            "attribution": {
                key: attribution[key]
                for key in ("target_skill", "failure_kind", "content")
                if key in attribution
            },
            "next_step": "human_memory_review",
        }

    def feedback(
        self,
        task_id: str,
        decision: str,
        note: str,
        corrected_sql: str,
        *,
        user_id: str,
        session_id: str,
    ) -> Mapping[str, Any]:
        if decision not in {"correct", "incorrect"}:
            raise ValueError("feedback decision must be correct or incorrect")
        if decision == "incorrect" and not note.strip():
            raise ValueError("rejection reason is required")
        snapshot = self._snapshot()
        with Text2SQLEvolutionStore(self.evolution_store_path, snapshot) as evolution:
            trace = _trace_with_compiled_policy_sources(
                evolution,
                evolution.get_query_trace(task_id),
            )
            if (
                str(trace.get("user_id") or "") != user_id
                or str(trace.get("session_id") or "") != session_id
            ):
                raise PermissionError("query task does not belong to this session")
            if not _production_feedback_eligible(trace):
                human_decision = evolution.record_query_feedback(
                    task_id, decision, note, user_id
                )
                return {
                    "task_id": task_id,
                    "feedback": decision,
                    "decision": dict(human_decision),
                    "experience_id": "",
                    "memory_id": "",
                    "attribution": {},
                    "experience_skipped_reason": _feedback_skip_reason(trace),
                    "source_origin": str(trace.get("origin") or ""),
                    "source_lane": str(trace.get("source_lane") or ""),
                    "next_step": "feedback_recorded",
                }
            experience_id = ""
            memory_id = ""
            attribution: Mapping[str, Any] = {}
            if decision == "correct":
                if trace.get("status") != "success" or not trace.get("final_sql"):
                    raise ValueError("only a successful SQL query can be confirmed")
                gate = validate_sql(str(trace["final_sql"]), snapshot)
                if not gate.accepted:
                    raise ValueError("stored SQL no longer passes the deterministic gate")
                experience_id = evolution.add_experience_candidate(
                    task_id,
                    str(trace.get("standalone_question") or trace.get("question") or ""),
                    str(trace["final_sql"]),
                    source_kind="human_confirmed_query",
                    eligible=True,
                )
            elif corrected_sql.strip():
                gate = validate_sql(corrected_sql, snapshot)
                if not gate.accepted:
                    raise ValueError(
                        "corrected SQL failed the deterministic gate: %s"
                        % ", ".join(gate.errors)
                    )
                experience_id = evolution.add_experience_candidate(
                    task_id,
                    str(trace.get("standalone_question") or trace.get("question") or ""),
                    corrected_sql,
                    source_kind="human_corrected_sql",
                    eligible=True,
                )
            if decision == "incorrect":
                attribution = attribute_query_failure(
                    trace,
                    snapshot,
                    corrected_sql=corrected_sql,
                    feedback_note=note,
                )
                failure_kind = str(attribution.get("failure_kind") or "")
                confident_owner = bool(
                    corrected_sql.strip()
                    or failure_kind != "critic_false_accept"
                    or "critic" in note.casefold()
                    or "评审" in note
                )
                feedback_evidence: dict[str, Any] = {
                    "decision": "incorrect",
                    "note": note,
                    "corrected_sql": corrected_sql,
                }
                if confident_owner:
                    feedback_evidence.update(
                        {
                            "target_agent": str(
                                attribution.get("target_skill") or ""
                            ),
                            "problem_code": failure_kind,
                            "correction": str(
                                (attribution.get("rule") or {}).get("action")
                                or attribution.get("content")
                                or ""
                            ),
                        }
                    )
                extracted = extract_user_correction_experiences(
                    trace,
                    feedback_evidence,
                    snapshot=snapshot,
                )
                if extracted:
                    memory_id = evolution.add_experience_memory(
                        extracted[0], origin_split="production_feedback"
                    )
            human_decision = evolution.record_query_feedback(
                task_id, decision, note, user_id
            )
            result = {
                "task_id": task_id,
                "feedback": decision,
                "decision": dict(human_decision),
                "experience_id": experience_id,
                "memory_id": memory_id,
                "attribution": {
                    key: attribution[key]
                    for key in ("target_skill", "failure_kind", "content")
                    if key in attribution
                },
                "next_step": (
                    "human_experience_and_memory_review"
                    if experience_id and memory_id
                    else "human_experience_review"
                    if experience_id
                    else "human_memory_review"
                    if memory_id
                    else "completed"
                ),
            }
        if decision == "correct" and experience_id:
            promotion = self.confirm_experience(experience_id, user_id, note)
            result.update(
                {
                    "experience": dict(promotion),
                    "next_step": "available_in_vanna",
                }
            )
        return result

    def review_memory_candidate(
        self,
        memory_id: str,
        decision: str,
        actor: str,
        *,
        target_skill: str = "",
        failure_kind: str = "",
        content: str = "",
        rule: Optional[Mapping[str, Any]] = None,
        review_note: str = "",
    ) -> Mapping[str, Any]:
        snapshot = self._snapshot()
        with Text2SQLEvolutionStore(self.evolution_store_path, snapshot) as evolution:
            current = evolution.get_memory(memory_id)
            is_experience = (
                (current.get("rule") or {}).get("contract")
                == "ExperienceMemory/v1"
            )
            if is_experience:
                experience_decision = {
                    "approve": "confirm",
                    "confirm": "confirm",
                    "reject": "reject",
                    "needs_evidence": "needs_evidence",
                }.get(decision)
                if not experience_decision:
                    raise ValueError("invalid Experience review decision")
                reviewed = evolution.review_experience_memory(
                    memory_id,
                    experience_decision,
                    actor,
                    review_note,
                )
            else:
                if decision == "approve":
                    evolution.update_memory_candidate(
                        memory_id,
                        target_skill,
                        failure_kind,
                        content,
                        rule=rule,
                    )
                reviewed = evolution.review_memory(
                    memory_id,
                    decision,
                    actor,
                    human_reviewed=True,
                    review_note=review_note,
                )
            memory_snapshot_id = evolution.memory_snapshot_id
        return {
            **dict(reviewed),
            "memory_snapshot_id": memory_snapshot_id,
            "next_step": (
                "select_for_policy_candidate"
                if reviewed["state"] == "confirmed"
                else "supply_additional_evidence"
                if reviewed["state"] == "needs_evidence"
                else "experience_rejected"
                if is_experience
                else
                "run_240_case_memory_evaluation"
                if reviewed["state"] == "approved"
                else "memory_rejected"
            ),
        }

    def start_memory_evaluation(
        self,
        memory_id: str,
        actor: str,
        principals: Sequence[str] = ("local-user",),
    ) -> Mapping[str, Any]:
        if not self.model_ready:
            raise RuntimeError("configured LLM is required for memory evaluation")
        snapshot = self._snapshot()
        bundle = load_dataset(
            self.dataset_path, REQUIRED_MEMORY_EVALUATION_SPLITS
        )
        if (
            sum(bundle.split_counts.values()) != 240
            or not bundle.review_evidence.get("verified")
        ):
            raise ValueError("memory evaluation requires the reviewed 240-case dataset")
        wiki_version, vanna_ready = self._runtime_vanna_pin(snapshot)
        evaluation_principals = tuple(
            sorted(set([*(str(value) for value in principals), "local-user"]))
        )
        evaluation_root = PROJECT_ROOT / "artifacts" / "text2sql" / "evaluation"
        with Text2SQLEvolutionStore(self.evolution_store_path, snapshot) as evolution:
            policy_version = evolution.active_policy_version
            stable_memory_snapshot = evolution.memory_snapshot_id
            expected_pins = {
                "database_snapshot_id": snapshot["snapshot_id"],
                "wiki_index_version": wiki_version,
                "vanna_index_version": (
                    wiki_version if vanna_ready else "fallback:%s" % wiki_version
                ),
                "memory_snapshot_id": stable_memory_snapshot,
                "policy_version": policy_version,
            }
            model = {
                "provider": str(self.llm_config["provider"]),
                "model": str(self.llm_config["model"]),
                "temperature": 0,
            }
            expected_runtime = build_runtime_identity(
                token_budget=self.settings.agent_token_budget,
                time_budget=self.settings.agent_time_budget_seconds,
                policy_source_memory_ids=evolution.policy_source_memory_ids(
                    policy_version
                ),
            )
            baseline = find_matching_baseline(
                evaluation_root,
                dataset_id=bundle.dataset_id,
                dataset_sha256=bundle.dataset_sha256,
                model=model,
                version_pins=expected_pins,
                runtime=expected_runtime,
                principals=evaluation_principals,
            )
            token = uuid.uuid4().hex[:12]
            job_root = evaluation_root / "memory-runs" / (
                "%s-%s" % (memory_id, token)
            )
            baseline_path = baseline or (job_root / "baseline-240.json")
            candidate_path = job_root / "candidate-240.json"
            log_path = job_root / "evaluation.log"
            job = evolution.create_memory_evaluation_job(
                memory_id,
                actor,
                str(baseline_path.resolve()),
                str(candidate_path.resolve()),
                str(log_path.resolve()),
                240 if baseline else 480,
            )
        job_root.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "run_text2sql_memory_evaluation.py"),
            "--job-id",
            str(job["job_id"]),
            "--memory-id",
            memory_id,
            "--dataset",
            str(self.dataset_path),
            "--snapshot",
            str(self.snapshot_path),
            "--evolution-store",
            str(self.evolution_store_path),
            "--workers",
            str(max(1, min(int(self.settings.async_workers), 4))),
        ]
        for principal in evaluation_principals:
            command.extend(("--principal", principal))
        try:
            subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except Exception as exc:
            with Text2SQLEvolutionStore(
                self.evolution_store_path, snapshot
            ) as evolution:
                evolution.update_memory_evaluation_job(
                    str(job["job_id"]),
                    status="failed",
                    phase="failed",
                    error=str(exc),
                )
            raise
        return {
            **{
                key: job[key]
                for key in (
                    "job_id",
                    "memory_id",
                    "status",
                    "phase",
                    "progress_current",
                    "progress_total",
                )
            },
            "baseline_reused": bool(baseline),
            "background": True,
        }

    def activate_memory_candidate(
        self,
        memory_id: str,
        actor: str,
        reason: str,
        principals: Sequence[str] = ("local-user",),
    ) -> Mapping[str, Any]:
        if not self.model_ready:
            raise RuntimeError("configured LLM is required for Memory activation")
        snapshot = self._snapshot()
        evaluation_principals = tuple(
            sorted(set([*(str(value) for value in principals), "local-user"]))
        )
        wiki_version, vanna_ready = self._runtime_vanna_pin(snapshot)
        with Text2SQLEvolutionStore(self.evolution_store_path, snapshot) as evolution:
            policy_version = evolution.active_policy_version
            current_pins = {
                "database_snapshot_id": snapshot["snapshot_id"],
                "wiki_index_version": wiki_version,
                "vanna_index_version": (
                    wiki_version if vanna_ready else "fallback:%s" % wiki_version
                ),
                "memory_snapshot_id": evolution.memory_snapshot_id,
                "policy_version": policy_version,
            }
            item = evolution.activate_memory(
                memory_id,
                actor,
                reason,
                human_approved=True,
                current_version_pins=current_pins,
                current_evaluation_identity={
                    "model": {
                        "provider": str(self.llm_config["provider"]),
                        "model": str(self.llm_config["model"]),
                        "temperature": 0,
                    },
                    "runtime": dict(
                        build_runtime_identity(
                            token_budget=self.settings.agent_token_budget,
                            time_budget=self.settings.agent_time_budget_seconds,
                            policy_source_memory_ids=evolution.policy_source_memory_ids(
                                policy_version
                            ),
                        )
                    ),
                    "principals": list(evaluation_principals),
                },
            )
            memory_snapshot_id = evolution.memory_snapshot_id
        return {**dict(item), "memory_snapshot_id": memory_snapshot_id}

    def rollback_memory(
        self, memory_id: str, actor: str, reason: str
    ) -> Mapping[str, Any]:
        snapshot = self._snapshot()
        with Text2SQLEvolutionStore(self.evolution_store_path, snapshot) as evolution:
            item = evolution.rollback_memory(memory_id, actor, reason)
            memory_snapshot_id = evolution.memory_snapshot_id
        return {**dict(item), "memory_snapshot_id": memory_snapshot_id}



    def _remember_trace_legacy(
        self,
        result: Mapping[str, Any],
        internal: Optional[Mapping[str, Any]] = None,
        *,
        user_id: str = "local-user",
        session_id: str = "default",
    ) -> None:
        internal = dict(internal or {})
        collaboration = dict(
            internal.get("collaboration") or result.get("collaboration") or {}
        )
        workers = {
            str(item.get("worker") or ""): item
            for item in collaboration.get("worker_results") or ()
            if isinstance(item, Mapping)
        }
        grounding = dict((workers.get("schema-grounding") or {}).get("output") or {})
        planning_worker = workers.get("query-planning") or workers.get("sql-strategy") or {}
        planning = dict(planning_worker.get("output") or {})
        generation = dict(collaboration.get("sql_generation_result") or {})
        # Read old traces without reintroducing the combined role into the new
        # public protocol. New runs always use the independent collaboration field.
        if not generation and workers.get("sql-strategy"):
            legacy_output = dict((workers["sql-strategy"].get("output") or {}))
            generation = {
                "worker": "sql-generation",
                "status": str(workers["sql-strategy"].get("status") or "unknown"),
                "memory_evidence_ids": list(
                    workers["sql-strategy"].get("memory_evidence_ids") or ()
                ),
                "output": {
                    "sql_candidates": list(legacy_output.get("sql_candidates") or ()),
                    "generation_notes": ["legacy combined sql-strategy trace"],
                },
            }
            collaboration["sql_generation_result"] = generation
        draft = dict(collaboration.get("draft_link_pack") or {})
        public_draft = {
            key: draft.get(key)
            for key in (
                "contract",
                "trust",
                "draft_sql",
                "draft_valid",
                "draft_error",
                "tables",
                "columns",
                "projection_columns",
                "filter_columns",
                "group_columns",
                "order_columns",
                "join_columns",
                "unresolved_columns",
                "ambiguous_columns",
                "column_owners",
                "has_star",
                "joins",
                "links",
                "logical_concepts",
                "draft_output",
                "forward_linking",
                "semantic_completion",
                "coverage",
            )
        } if draft else {}
        retrieval = []
        for role in ("schema-grounding", "query-planning"):
            worker = workers.get(role)
            if worker is None and role == "query-planning":
                worker = workers.get("sql-strategy")
            for item in (worker or {}).get("retrieval") or ():
                if isinstance(item, Mapping):
                    retrieval.append({"role": role, **dict(item)})
        memory_sources = [
            ("text2sql-lead", "delegation", collaboration.get("lead_delegation")),
            ("text2sql-lead", "assessment", collaboration.get("lead_assessment")),
            ("text2sql-critic", "critique", collaboration.get("critic_result")),
            ("text2sql-lead", "selection", collaboration.get("lead_final")),
        ]
        memory_sources.extend(
            (
                ("schema-grounding", "worker", workers.get("schema-grounding")),
                ("query-planning", "worker", planning_worker),
                ("sql-generation", "worker", generation),
            )
        )
        for role, phase, payload in memory_sources:
            if not isinstance(payload, Mapping):
                continue
            memory_ids = list(
                dict.fromkeys(
                    str(item)
                    for item in payload.get("memory_evidence_ids") or ()
                    if item
                )
            )
            if memory_ids:
                retrieval.append(
                    {
                        "role": role,
                        "phase": phase,
                        "backend": "semantic-memory",
                        "memory_ids": memory_ids,
                    }
                )
        answer = dict(result.get("answer") or {})
        trace = {
            "task_id": result.get("task_id"),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "status": result.get("status"),
            "question": result.get("question"),
            "original_question": result.get("question"),
            "standalone_question": result.get("standalone_question"),
            "query_type": result.get("query_type"),
            "parent_task_id": result.get("parent_query_run_id"),
            "user_id": user_id,
            "session_id": session_id,
            "final_sql": result.get("final_sql"),
            "gates": dict(result.get("gates") or {}),
            "agents": list(result.get("agents") or ()),
            "execution": dict(result.get("execution") or {}),
            "version_pins": dict(result.get("version_pins") or {}),
            "schema_plan": dict(grounding.get("schema_plan") or {}),
            "query_spec": dict(planning.get("query_spec") or {}),
            "draft_link_pack": public_draft,
            "collaboration": collaboration,
            "retrieval": retrieval,
            "result_rows": list(answer.get("rows") or ())[:50],
            "answer": {
                "columns": list(answer.get("columns") or ()),
                "row_count": int(answer.get("row_count") or 0),
                "truncated": bool(answer.get("truncated")),
                "summary_text": str(answer.get("summary_text") or "")[:2000],
            },
        }
        snapshot = self._snapshot()
        with Text2SQLEvolutionStore(self.evolution_store_path, snapshot) as evolution:
            evolution.save_query_trace(trace)
            status = str(result.get("status") or "failed")
            evolution.append_message(
                user_id,
                session_id,
                "assistant",
                str(answer.get("summary_text") or result.get("final_sql") or status),
                str(result.get("task_id") or ""),
            )
            gate = dict(result.get("gates") or {})
            if (
                status == "success"
                and str(result.get("query_type") or "DATA_QUERY") == "DATA_QUERY"
                and str(result.get("final_sql") or "").strip()
            ):
                reasons = []
                if not gate.get("accepted"):
                    reasons.append("deterministic_gate_not_accepted")
                if int(answer.get("row_count") or 0) <= 0:
                    reasons.append("empty_result_requires_manual_confirmation")
                evolution.add_experience_candidate(
                    str(result.get("task_id") or ""),
                    str(result.get("standalone_question") or result.get("question") or ""),
                    str(result.get("final_sql") or ""),
                    eligible=False,
                    eligibility_reasons=[*reasons, "requires_human_feedback"],
                )
            # Mark the external request complete only after every idempotent
            # trace/message/experience side effect has committed.
            evolution.finish_query_attempt(
                str(result.get("task_id") or ""),
                "completed",
                response=result,
            )

    def _remember_trace(
        self,
        result: Mapping[str, Any],
        internal: Optional[Mapping[str, Any]] = None,
        *,
        user_id: str = "local-user",
        session_id: str = "default",
    ) -> Mapping[str, Any]:
        """Finalize a Web QueryRun without letting Memory replace its answer."""

        task_id = str(result.get("task_id") or "")[:200]
        release = dict(result.get("release") or {})
        release_lane = str(release.get("lane") or "").casefold()
        if bool(release.get("candidate_output_used")) or release_lane in {
            "candidate",
            "canary",
        }:
            source_lane = "candidate"
        elif bool(release.get("shadow_sampled")):
            source_lane = "shadow"
        else:
            source_lane = "stable"
        answer = dict(result.get("answer") or {})
        side_effect_errors: list[str] = []
        try:
            snapshot = self._snapshot()
            with Text2SQLEvolutionStore(
                self.evolution_store_path, snapshot
            ) as evolution:
                write_status = finalize_run(
                    result,
                    internal,
                    store=evolution,
                    task_id=task_id,
                    user_id=user_id,
                    session_id=session_id,
                    origin="web",
                    source_lane=source_lane,
                    source_revision=evolution.next_query_trace_revision(
                        task_id
                    ),
                )
                try:
                    evolution.append_message(
                        user_id,
                        session_id,
                        "assistant",
                        str(
                            answer.get("summary_text")
                            or result.get("final_sql")
                            or result.get("status")
                            or "failed"
                        ),
                        task_id,
                    )
                except Exception as exc:
                    side_effect_errors.append(
                        "working_memory:%s" % str(exc)[:300]
                    )
                try:
                    gate = dict(result.get("gates") or {})
                    if (
                        str(result.get("status") or "") == "success"
                        and str(result.get("query_type") or "DATA_QUERY")
                        == "DATA_QUERY"
                        and str(result.get("final_sql") or "").strip()
                    ):
                        reasons = []
                        if not gate.get("accepted"):
                            reasons.append("deterministic_gate_not_accepted")
                        if int(answer.get("row_count") or 0) <= 0:
                            reasons.append(
                                "empty_result_requires_manual_confirmation"
                            )
                        evolution.add_experience_candidate(
                            task_id,
                            str(
                                result.get("standalone_question")
                                or result.get("question")
                                or ""
                            ),
                            str(result.get("final_sql") or ""),
                            eligible=False,
                            eligibility_reasons=[
                                *reasons,
                                "requires_human_feedback",
                            ],
                        )
                except Exception as exc:
                    side_effect_errors.append(
                        "question_sql_review:%s" % str(exc)[:300]
                    )
                status_payload = dict(write_status.as_dict())
                if side_effect_errors:
                    status_payload["memory_status"] = "degraded"
                    status_payload["error"] = "; ".join(
                        [
                            str(status_payload.get("error") or ""),
                            *side_effect_errors,
                        ]
                    ).strip("; ")[:1000]
                try:
                    evolution.finish_query_attempt(
                        task_id,
                        "completed",
                        response={**dict(result), **status_payload},
                    )
                except Exception as exc:
                    status_payload["memory_status"] = "degraded"
                    status_payload["error"] = "; ".join(
                        [
                            str(status_payload.get("error") or ""),
                            "query_attempt:%s" % str(exc)[:300],
                        ]
                    ).strip("; ")[:1000]
                return status_payload
        except Exception as exc:
            return MemoryWriteStatus(
                status="degraded",
                trace_recorded=False,
                task_id=task_id,
                origin="web",
                source_lane=source_lane,
                error="%s: %s" % (type(exc).__name__, str(exc)[:500]),
            ).as_dict()

    @staticmethod
    def _agent_trace(result: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
        collaboration = result.get("collaboration") or {}
        route = dict(collaboration.get("route") or {})
        route_type = str(route.get("type") or result.get("query_type") or "DATA_QUERY")
        trace: list[Mapping[str, Any]] = [
            {
                "role": "text2sql-lead",
                "stage": "query-routing",
                "status": "completed",
                "summary": (
                    "%s · %s"
                    % (
                        route_type,
                        str(route.get("reason") or "Leader 已完成查询路由"),
                    )
                )[:500],
                "detail": {
                    "query_type": route_type,
                    "parent_query_run_id": str(
                        route.get("parent_query_run_id") or ""
                    ),
                },
            }
        ]
        if route_type == "CLARIFICATION":
            trace[0] = {**trace[0], "status": "needs_clarification",
                        "summary": "；".join((collaboration.get("clarification") or {}).get("questions") or ())}
            return tuple(trace)
        if route_type == "RESULT_QA":
            final = dict(collaboration.get("lead_final") or {})
            trace.append(
                {
                    "role": "text2sql-lead",
                    "stage": "cached-result-answer",
                    "status": "completed",
                    "summary": str(
                        final.get("reasoning_summary")
                        or "基于授权的历史 QueryRun 结果回答，未生成或执行 SQL"
                    )[:500],
                    "detail": {
                        "requires_new_query": bool(final.get("requires_new_query"))
                    },
                }
            )
            return tuple(trace)
        workers = {
            str(item.get("worker") or ""): item
            for item in collaboration.get("worker_results") or ()
            if isinstance(item, Mapping)
        }
        for role in ("schema-grounding", "query-planning"):
            worker = workers.get(role) or (
                workers.get("sql-strategy") if role == "query-planning" else {}
            )
            worker = dict(worker or {})
            output = dict(worker.get("output") or {})
            if role == "schema-grounding":
                plan = output.get("schema_plan") or {}
                detail = {
                    "tables": list(plan.get("tables") or ()),
                    "columns": list(plan.get("columns") or ()),
                    "join_count": len(plan.get("joins") or ()),
                }
                summary = (
                    "已完成表、字段、Join、业务值与结果粒度定位"
                    if worker
                    else "Schema Grounding 未运行"
                )
            else:
                spec = output.get("query_spec") or {}
                detail = {
                    "intent": spec.get("intent", ""),
                    "expected_shape": spec.get("expected_shape", ""),
                    "dimension_count": len(spec.get("dimensions") or ()),
                    "measure_count": len(spec.get("measures") or ()),
                    "filter_count": len(spec.get("filters") or ()),
                }
                summary = (
                    "已生成独立于物理表列的逻辑 QuerySpec"
                    if worker
                    else "Query Planning 未运行"
                )
            trace.append(
                {
                    "role": role,
                    "stage": "schema-grounding" if role == "schema-grounding" else "query-planning",
                    "status": str(worker.get("status") or "not-run"),
                    "summary": summary,
                    "detail": detail,
                    "evidence_count": len(worker.get("observed_evidence_ids") or ()),
                }
            )
        assessment = dict(
            collaboration.get("lead_plan_approval")
            or collaboration.get("lead_assessment")
            or {}
        )
        bound = dict(collaboration.get("bound_query_plan") or {})
        approved = dict(collaboration.get("approved_query_plan") or {})
        conflicts = list(collaboration.get("binding_conflicts") or ())
        trace.append(
            {
                "role": "text2sql-lead",
                "stage": "semantic-plan-approval",
                "status": "completed" if assessment and not assessment.get("skipped") else "not-run",
                "summary": str(
                    assessment.get("reasoning_summary")
                    or (
                        "已批准不可变 BoundQueryPlan"
                        if approved
                        else "查询计划未获批准"
                    )
                )[:500],
                "detail": {
                    "approved": bool(approved),
                    "bound_plan_fingerprint": str(bound.get("fingerprint") or ""),
                    "binding_conflict_count": len(conflicts),
                    "revisions_applied": int(
                        collaboration.get("revisions_applied") or 0
                    ),
                },
            }
        )
        generation = dict(collaboration.get("sql_generation_result") or {})
        if not generation and workers.get("sql-strategy"):
            legacy = dict((workers["sql-strategy"].get("output") or {}))
            generation = {
                "status": workers["sql-strategy"].get("status") or "unknown",
                "output": {
                    "sql_candidates": legacy.get("sql_candidates") or (),
                    "generation_notes": ["legacy combined sql-strategy trace"],
                },
            }
        generation_output = dict(generation.get("output") or {})
        generation_notes = [
            str(item)[:500]
            for item in (generation_output.get("generation_notes") or ())[:20]
        ]
        trace.append(
            {
                "role": "sql-generation",
                "stage": "sql-generation",
                "status": str(generation.get("status") or "not-run"),
                "summary": (
                    generation_notes[0]
                    if generation_notes
                    else "已根据 ApprovedQueryPlan 生成 SQL 候选"
                    if generation
                    else "SQL Generation 未运行"
                ),
                "detail": {
                    "candidate_count": len(
                        generation_output.get("sql_candidates") or ()
                    ),
                    "generation_notes": generation_notes,
                    "repair_count": int(
                        collaboration.get("sql_generation_repairs") or 0
                    ),
                },
                "evidence_count": len(
                    generation.get("observed_evidence_ids") or ()
                ),
            }
        )
        critic = collaboration.get("critic_result") or {}
        decisions = critic.get("decisions") or ()
        trace.append(
            {
                "role": "text2sql-critic",
                "stage": "blind-review",
                "status": "completed" if critic else "not-run",
                "summary": str(critic.get("summary") or "已盲审 SQL 候选")[:500],
                "detail": {
                    "candidate_count": len(decisions),
                    "accepted_count": sum(bool(item.get("accepted")) for item in decisions),
                },
            }
        )
        final = collaboration.get("lead_final") or {}
        trace.append(
            {
                "role": ("text2sql-harness" if final.get("selection_method") == "deterministic_single_candidate"
                         else "text2sql-lead"),
                "stage": "final-selection",
                "status": "completed" if final and final.get("selection_method") != "skipped" else "not-run",
                "summary": str(
                    final.get("resolution_summary") or "已选择候选并交给确定性执行门禁"
                )[:500],
                "detail": {
                    "final_candidate_index": final.get("final_candidate_index"),
                    "selection_method": final.get("selection_method", "lead_multiple_candidates"),
                },
            }
        )
        return tuple(trace)

    @staticmethod
    def _public_result(result: Mapping[str, Any], task_id: str) -> Mapping[str, Any]:
        answer = result.get("answer") or {}
        execution = result.get("execution") or {}
        collaboration = dict(result.get("collaboration") or {})
        draft = dict(collaboration.get("draft_link_pack") or {})
        plan_payload = _public_plan_payload(collaboration)
        return {
            "task_id": task_id,
            "status": str(result.get("status") or "failed"),
            "question": str(result.get("question") or ""),
            "standalone_question": str(
                result.get("standalone_question") or result.get("question") or ""
            ),
            "query_type": str(result.get("query_type") or "DATA_QUERY"),
            "parent_query_run_id": str(result.get("parent_query_run_id") or ""),
            "final_sql": str(result.get("final_sql") or ""),
            "answer": {
                "columns": list(answer.get("columns") or ()),
                "rows": list(answer.get("rows") or ()),
                "row_count": int(answer.get("row_count") or 0),
                "truncated": bool(answer.get("truncated")),
                "summary_text": str(answer.get("summary_text") or "")[:2000],
            },
            "gates": dict(result.get("gates") or {}),
            "version_pins": dict(result.get("version_pins") or {}),
            "release": dict(result.get("release") or {}),
            "clarification": dict(result.get("clarification") or collaboration.get("clarification") or {}),
            "diagnostic": dict(result.get("diagnostic") or diagnose_result(result)),
            **plan_payload,
            "deterministic_runtime": _public_runtime_payload(
                collaboration, dict(result.get("gates") or {})
            ),
            "draft_link_pack": {
                key: draft.get(key)
                for key in (
                    "contract",
                    "trust",
                    "draft_sql",
                    "draft_valid",
                    "draft_error",
                    "tables",
                    "columns",
                    "projection_columns",
                    "filter_columns",
                    "group_columns",
                    "order_columns",
                    "join_columns",
                    "unresolved_columns",
                    "ambiguous_columns",
                    "column_owners",
                    "has_star",
                    "joins",
                    "links",
                    "logical_concepts",
                    "draft_output",
                    "forward_linking",
                    "semantic_completion",
                    "coverage",
                )
            } if draft else {},
            "agents": list(Text2SQLWebService._agent_trace(result)),
            "execution": {
                "llm_calls": int(execution.get("llm_calls") or 0),
                "tool_calls": int(execution.get("tool_calls") or 0),
                "input_tokens": int(execution.get("input_tokens") or 0),
                "output_tokens": int(execution.get("output_tokens") or 0),
                "total_tokens": int(execution.get("total_tokens") or 0),
                "cost_usd": float(execution.get("cost_usd") or 0.0),
                "duration_ms": int(execution.get("duration_ms") or 0),
            },
        }

    def query(
        self,
        question: str,
        principals: Sequence[str] = ("local-user",),
        task_id: str = "",
        session_id: str = "default",
        clarification_task_id: str = "",
    ) -> Mapping[str, Any]:
        question = question.strip()
        if not question:
            raise ValueError("Text2SQL question is required")
        if len(question) > 2000:
            raise ValueError("Text2SQL question exceeds 2000 characters")
        if not self.model_ready:
            raise RuntimeError(
                "阿里云百炼模型尚未配置：请设置 EVOAGENT_DASHSCOPE_API_KEY 后重启服务"
            )
        snapshot = self._snapshot()
        query_task_id = task_id.strip() or "text2sql-web-%s" % uuid.uuid4().hex
        session_id = session_id.strip()[:200] or "default"
        user_id = str(principals[0] if principals else "local-user")[:200]
        effective_principals = tuple(dict.fromkeys([*principals, "local-user"]))
        checkpoint_store = Text2SQLRuntimeCheckpointStore(
            self.checkpoint_store_path
        )
        with Text2SQLEvolutionStore(self.evolution_store_path, snapshot) as evolution:
            continuation = {}
            clarification_task_id = clarification_task_id.strip()
            if clarification_task_id:
                if len(clarification_task_id) > 200 or clarification_task_id == query_task_id:
                    raise ValueError("澄清回复必须使用新任务，并引用原澄清请求")
                try:
                    parent_trace = evolution.get_query_trace(clarification_task_id)
                except ValueError:
                    raise ValueError("澄清请求不存在或不属于当前用户和会话") from None
                question, continuation = clarification_continuation(
                    parent_trace, clarification_task_id, user_id, session_id, question
                )
            active_policy_version = evolution.active_policy_version
            memory_bundle = evolution.runtime_memory_snapshot()
            memory_snapshot_id = str(memory_bundle["memory_snapshot_id"])
            wiki_index_version, vanna_ready = self._runtime_vanna_pin(snapshot)
            version_pins = {
                "database_snapshot_id": snapshot["snapshot_id"],
                "wiki_index_version": wiki_index_version,
                "vanna_index_version": (
                    wiki_index_version
                    if vanna_ready
                    else "fallback:%s" % wiki_index_version
                ),
                "memory_snapshot_id": memory_snapshot_id,
                "policy_version": active_policy_version,
            }
            raw_context = evolution.recent_query_context(user_id, session_id, limit=4)
            # A retry may already have written its own attempt/trace. Excluding the
            # current task keeps the semantic context stable across process restarts.
            conversation_context = {
                "scope": {"user_id": user_id, "session_id": session_id},
                "recent_messages": [
                    dict(item)
                    for item in raw_context.get("recent_messages") or ()
                    if str(item.get("task_id") or "") != query_task_id
                ][-8:],
                "recent_query_runs": [
                    dict(item)
                    for item in raw_context.get("recent_query_runs") or ()
                    if str(item.get("task_id") or "") != query_task_id
                ][:3],
            }
            if continuation:
                conversation_context["clarification_continuation"] = continuation
            request_runtime_identity = self._query_attempt_runtime_identity(
                version_pins,
                conversation_context,
                evolution.policy_source_memory_ids(active_policy_version),
            )
            if continuation:
                request_runtime_identity = {
                    **request_runtime_identity, "clarification_task_id": clarification_task_id,
                }
            attempt = evolution.prepare_query_attempt(
                query_task_id,
                user_id,
                session_id,
                question,
                effective_principals,
                conversation_context,
                request_runtime_identity,
            )
            if attempt.get("cached_response") is not None:
                return dict(attempt["cached_response"])
            conversation_context = dict(attempt["conversation_context"])
            evolution.append_message(
                user_id, session_id, "user", question, query_task_id
            )

            def engine_for(version: str) -> Text2SQLAgenticEngine:
                policy = evolution.get_policy(version)

                def result_snapshot(parent_id: str) -> Mapping[str, Any]:
                    # Shadow/canary executes lanes on worker threads. Never reuse
                    # the main thread's SQLite connection from the evolution store.
                    with Text2SQLEvolutionStore(
                        self.evolution_store_path, snapshot
                    ) as lookup:
                        return lookup.query_result_snapshot(
                            parent_id, user_id, session_id
                        )

                return Text2SQLAgenticEngine(
                    client=self.client,
                    database_path=self.database_path,
                    snapshot=snapshot,

                    vanna_index_root=self.vanna_index_root,
                    vanna_index_version=wiki_index_version,
                    principals=effective_principals,
                    memory_snapshot_id=memory_snapshot_id,
                    policy_version=policy.version,
                    policy_artifact=policy,
                    policy_source_memory_ids=evolution.policy_source_memory_ids(
                        policy.version
                    ),
                    memory_snapshot_bundle=memory_bundle,
                    result_snapshot_provider=result_snapshot,
                    checkpoint_store=checkpoint_store,
                    token_budget=self.settings.agent_token_budget,
                    time_budget=self.settings.agent_time_budget_seconds,
                    max_rows=200,
                )

            try:
                stable_engine = engine_for(active_policy_version)
                stable_evaluation_identity = {
                    "model": {
                        "provider": str(
                            getattr(self.client, "provider", "unknown")
                        ),
                        "model": str(getattr(self.client, "model", "unknown")),
                        "temperature": 0,
                    },
                    "runtime": dict(stable_engine.runtime_identity),
                    "principals": sorted(set(effective_principals)),
                }
                release = Text2SQLShadowReleaseManager(evolution)

                def candidate_runner(version: str):
                    candidate = engine_for(version)
                    return lambda value: candidate.run(
                        value,
                        task_id="%s:candidate:%s" % (
                            query_task_id,
                            candidate.policy_version,
                        ),
                        conversation_context=conversation_context,
                    )

                result = release.execute(
                    question,
                    query_task_id,
                    lambda value: stable_engine.run(
                        value,
                        task_id="%s:stable:%s" % (
                            query_task_id,
                            stable_engine.policy_version,
                        ),
                        conversation_context=conversation_context,
                    ),
                    candidate_runner,
                    stable_engine.version_pins,
                    stable_evaluation_identity,
                )
            except Exception as exc:
                safe_error_code = "text2sql_runtime_error"
                failure_result = {
                    "task_id": query_task_id,
                    "status": "error",
                    "question": question,
                    "original_question": question,
                    "standalone_question": question,
                    "query_type": "DATA_QUERY",
                    "final_sql": "",
                    "gates": {
                        "accepted": False,
                        "errors": [safe_error_code],
                    },
                    "answer": {
                        "columns": [],
                        "rows": [],
                        "row_count": 0,
                        "truncated": False,
                        "summary_text": "",
                    },
                    "version_pins": version_pins,
                }
                failure_internal = {
                    "collaboration": {
                        "diagnostic": {
                            "exception_type": type(exc).__name__[:200],
                        }
                    }
                }
                try:
                    finalize_run(
                        failure_result,
                        failure_internal,
                        store=evolution,
                        task_id=query_task_id,
                        user_id=user_id,
                        session_id=session_id,
                        origin="web",
                        source_lane="stable",
                        source_revision=evolution.next_query_trace_revision(
                            query_task_id
                        ),
                    )
                except Exception:
                    # Trace finalization is best effort and must not replace the
                    # runtime exception observed by the caller.
                    pass
                try:
                    evolution.finish_query_attempt(
                        query_task_id,
                        "error",
                        safe_error_code,
                    )
                except Exception:
                    pass
                raise
        public = self._public_result(result, query_task_id)
        memory_status = self._remember_trace(
            public,
            result,
            user_id=user_id,
            session_id=session_id,
        )
        return {**dict(public), **dict(memory_status)}
