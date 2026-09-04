"""Local Web-facing facade for the Text2SQL Multi-Agent runtime."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from ..config import Settings
from ..llm import JsonChatClient
from .agentic import (
    TEXT2SQL_PROTOCOL,
    TEXT2SQL_RUNTIME_NODES,
    Text2SQLAgenticEngine,
)
from .checkpoint_store import Text2SQLRuntimeCheckpointStore
from .database_tools import ROLE_TOOL_PERMISSIONS
from .evaluation import load_dataset
from .evolution import Text2SQLEvolutionStore
from .knowledge_store import KnowledgeStore
from .shadow import Text2SQLShadowReleaseManager
from .sql_safety import validate_sql
from .vanna_retriever import VannaRetrieverOnly


PROJECT_ROOT = Path(__file__).resolve().parents[2]

SKILL_DESCRIPTIONS = {
    "text2sql-lead": "拆解问题、并行调度角色、汇总证据并选择最终 SQL 候选。",
    "schema-grounding": "定位表、字段、关联关系、业务取值和查询结果粒度。",
    "sql-strategy": "根据已验证的 Schema 与知识生成只读 SQL 候选。",
    "text2sql-critic": "独立盲审 SQL 候选，对语义错误和安全风险执行否决。",
}


def _project_path(environment_name: str, default: str) -> Path:
    value = Path(os.getenv(environment_name, default))
    return value.resolve() if value.is_absolute() else (PROJECT_ROOT / value).resolve()


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
        knowledge_store_path: Optional[Path] = None,
        vanna_index_root: Optional[Path] = None,
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
        self.knowledge_store_path = knowledge_store_path or _project_path(
            "EVOAGENT_TEXT2SQL_KNOWLEDGE_STORE",
            "artifacts/text2sql/knowledge/knowledge.sqlite3",
        )
        self.vanna_index_root = vanna_index_root or _project_path(
            "EVOAGENT_TEXT2SQL_VANNA_ROOT",
            "artifacts/text2sql/vanna",
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

    def status(self) -> Mapping[str, Any]:
        snapshot = self._snapshot()
        dataset_status: dict[str, Any]
        try:
            dataset = load_dataset(self.dataset_path)
            dataset_status = {
                "dataset_id": dataset.dataset_id,
                "dataset_sha256": dataset.dataset_sha256,
                "case_count": sum(dataset.split_counts.values()),
                "split_counts": dict(dataset.split_counts),
                "review_verified": bool(dataset.review_evidence.get("verified")),
                "reviewed_case_count": int(
                    dataset.review_evidence.get("reviewed_case_count") or 0
                ),
                "certificate_sha256": str(
                    dataset.review_evidence.get("certificate_sha256") or ""
                ),
            }
        except Exception as exc:
            dataset_status = {"review_verified": False, "error": str(exc)[:500]}
        with KnowledgeStore(self.knowledge_store_path) as knowledge:
            knowledge_status = dict(knowledge.stats())
            stable_index_version = knowledge.current_index_version("stable")
        vanna_status = dict(
            VannaRetrieverOnly(
                self.vanna_index_root,
                stable_index_version,
            ).status()
        )
        with Text2SQLEvolutionStore(self.evolution_store_path, snapshot) as evolution:
            release = Text2SQLShadowReleaseManager(evolution).status()
            experience_counts = {
                state: len(evolution.list_experiences(state, 100))
                for state in ("candidate", "ineligible", "promoted", "rejected")
            }
            evolution_status = {
                "active_policy_version": evolution.active_policy_version,
                "memory_snapshot_id": evolution.memory_snapshot_id,
                "stable_memory_count": len(evolution.list_memory("stable")),
                "candidate_memory_count": len(evolution.list_memory("candidate")),
                "experience_counts": experience_counts,
                "release": release,
            }
        return {
            "ready": self.model_ready
            and self.database_path.exists()
            and bool(dataset_status.get("review_verified")),
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
            "knowledge": knowledge_status,
            "vanna": vanna_status,
            "evolution": evolution_status,
            "roles": [
                "text2sql-lead",
                "schema-grounding",
                "sql-strategy",
                "text2sql-critic",
            ],
        }

    def skills(self) -> Mapping[str, Any]:
        """Return the active, role-scoped Text2SQL Skill catalog."""
        snapshot = self._snapshot()
        with Text2SQLEvolutionStore(self.evolution_store_path, snapshot) as evolution:
            policy = evolution.get_policy()
            policies = evolution.list_policies()
            skills = []
            for name, default_tools in ROLE_TOOL_PERMISSIONS.items():
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
                "submission_contract": "text2sql-role-skill-v1",
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
        if skill_name not in ROLE_TOOL_PERMISSIONS:
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
                    "contract": "text2sql-role-skill-v1",
                },
            )
            return {
                "skill_name": skill_name,
                "candidate_policy_version": version,
                "parent_policy_version": evolution.active_policy_version,
                "status": "candidate",
                "next_step": "run_validation_and_sealed_holdout",
            }

    def traces(self, limit: int = 20) -> Mapping[str, Any]:
        """Return bounded, public Text2SQL traces from the local control database."""
        bounded = max(1, min(int(limit), 50))
        snapshot = self._snapshot()
        with Text2SQLEvolutionStore(self.evolution_store_path, snapshot) as evolution:
            values = []
            for stored in evolution.list_query_traces(bounded):
                item = dict(stored)
                for private_key in (
                    "result_rows",
                    "collaboration",
                    "user_id",
                    "session_id",
                ):
                    item.pop(private_key, None)
                values.append(item)
        return {"traces": values, "retention": "local-sqlite", "limit": bounded}

    def experiences(self, state: str = "", limit: int = 50) -> Mapping[str, Any]:
        snapshot = self._snapshot()
        with Text2SQLEvolutionStore(self.evolution_store_path, snapshot) as evolution:
            values = list(evolution.list_experiences(state, limit))
        return {"experiences": values, "state": state or "all"}

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
        snapshot = self._snapshot()
        with Text2SQLEvolutionStore(self.evolution_store_path, snapshot) as evolution:
            trace = evolution.get_query_trace(task_id)
            if (
                str(trace.get("user_id") or "") != user_id
                or str(trace.get("session_id") or "") != session_id
            ):
                raise PermissionError("query task does not belong to this session")
            evolution.record_query_feedback(task_id, decision, note)
            experience_id = ""
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
            return {
                "task_id": task_id,
                "feedback": decision,
                "experience_id": experience_id,
                "next_step": (
                    "human_experience_review" if experience_id else "failure_attribution"
                ),
            }

    def review_experience(
        self, experience_id: str, decision: str, actor: str
    ) -> Mapping[str, Any]:
        snapshot = self._snapshot()
        with Text2SQLEvolutionStore(self.evolution_store_path, snapshot) as evolution:
            item = evolution.get_experience(experience_id)
            if decision == "reject":
                return dict(evolution.review_experience(experience_id, decision, actor))
            gate = validate_sql(str(item["sql"]), snapshot)
            if not gate.accepted:
                raise ValueError(
                    "experience SQL failed the deterministic gate: %s"
                    % ", ".join(gate.errors)
                )
            with KnowledgeStore(self.knowledge_store_path) as knowledge:
                promoted = knowledge.promote_verified_example(
                    str(item["question"]),
                    str(item["sql"]),
                    actor,
                    source_id=experience_id,
                    dependencies=tuple([*gate.tables, *gate.columns]),
                )
            reviewed = evolution.review_experience(
                experience_id,
                "approve",
                actor,
                str(promoted["evidence_id"]),
            )
        return {
            **dict(reviewed),
            "knowledge": dict(promoted),
            "vanna_rebuild_required": True,
            "next_step": "build_vanna_candidate_then_run_240_case_regression",
        }

    def _remember_trace(
        self,
        result: Mapping[str, Any],
        internal: Optional[Mapping[str, Any]] = None,
        *,
        user_id: str = "local-user",
        session_id: str = "default",
    ) -> None:
        internal = dict(internal or {})
        collaboration = dict(internal.get("collaboration") or {})
        workers = {
            str(item.get("worker") or ""): item
            for item in collaboration.get("worker_results") or ()
            if isinstance(item, Mapping)
        }
        grounding = dict((workers.get("schema-grounding") or {}).get("output") or {})
        strategy = dict((workers.get("sql-strategy") or {}).get("output") or {})
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
                "joins",
                "coverage",
            )
        } if draft else {}
        retrieval = [
            {"role": role, **dict(item)}
            for role, worker in workers.items()
            for item in worker.get("retrieval") or ()
            if isinstance(item, Mapping)
        ]
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
            "query_spec": dict(strategy.get("query_spec") or {}),
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
        draft = dict(collaboration.get("draft_link_pack") or {})
        if draft:
            coverage = dict(draft.get("coverage") or {})
            trace.append(
                {
                    "role": "vanna-draft-planner",
                    "stage": "draft-schema-linking",
                    "status": "completed" if draft.get("draft_valid") else "fallback",
                    "summary": (
                        "Vanna 辅助草案已由 AST 反解析；随后用问题直连字段和完整 DDL 补齐"
                        if draft.get("draft_valid")
                        else "Vanna 草案不可用，已回退到问题直连字段和完整 DDL"
                    ),
                    "detail": {
                        "tables": list(draft.get("tables") or ()),
                        "columns": list(draft.get("columns") or ()),
                        "join_count": len(draft.get("joins") or ()),
                        "has_full_ddl": bool(coverage.get("has_full_ddl")),
                    },
                }
            )
        for worker in collaboration.get("worker_results") or ():
            output = worker.get("output") or {}
            role = str(worker.get("worker") or "worker")
            if role == "schema-grounding":
                plan = output.get("schema_plan") or {}
                detail = {
                    "tables": list(plan.get("tables") or ()),
                    "columns": list(plan.get("columns") or ()),
                    "join_count": len(plan.get("joins") or ()),
                }
                summary = "已完成表、字段、Join 与结果粒度定位"
            else:
                spec = output.get("query_spec") or {}
                detail = {
                    "intent": spec.get("intent", ""),
                    "candidate_count": len(output.get("sql_candidates") or ()),
                }
                summary = "已生成只读 SQL 候选并执行 AST/EXPLAIN 检查"
            trace.append(
                {
                    "role": role,
                    "stage": "worker",
                    "status": str(worker.get("status") or "unknown"),
                    "summary": summary,
                    "detail": detail,
                    "evidence_count": len(worker.get("observed_evidence_ids") or ()),
                }
            )
        critic = collaboration.get("critic_result") or {}
        decisions = critic.get("decisions") or ()
        trace.append(
            {
                "role": "text2sql-critic",
                "stage": "blind-review",
                "status": "completed",
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
                "role": "text2sql-lead",
                "stage": "final-selection",
                "status": "completed",
                "summary": str(
                    final.get("resolution_summary") or "已选择候选并交给确定性执行门禁"
                )[:500],
                "detail": {
                    "final_candidate_index": final.get("final_candidate_index")
                },
            }
        )
        return tuple(trace)

    @staticmethod
    def _public_result(result: Mapping[str, Any], task_id: str) -> Mapping[str, Any]:
        answer = result.get("answer") or {}
        execution = result.get("execution") or {}
        draft = dict((result.get("collaboration") or {}).get("draft_link_pack") or {})
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
                    "joins",
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
            active_policy_version = evolution.active_policy_version
            memory_snapshot_id = evolution.memory_snapshot_id
            with KnowledgeStore(self.knowledge_store_path) as knowledge:
                wiki_index_version = knowledge.current_index_version("stable")
            vanna_ready = bool(
                VannaRetrieverOnly(
                    self.vanna_index_root, wiki_index_version
                ).status().get("ready")
            )
            request_runtime_identity = {
                "version_pins": {
                    "database_snapshot_id": snapshot["snapshot_id"],
                    "wiki_index_version": wiki_index_version,
                    "vanna_index_version": (
                        wiki_index_version
                        if vanna_ready
                        else "fallback:%s" % wiki_index_version
                    ),
                    "memory_snapshot_id": memory_snapshot_id,
                    "policy_version": active_policy_version,
                },
                "model": {
                    "provider": str(getattr(self.client, "provider", "unknown")),
                    "model": str(getattr(self.client, "model", "unknown")),
                    "temperature": 0,
                },
                "budgets": {
                    "token": self.settings.agent_token_budget,
                    "time": self.settings.agent_time_budget_seconds,
                    "max_rows": 200,
                    "timeout_ms": 3000,
                },
                "protocol": TEXT2SQL_PROTOCOL,
                "nodes": list(TEXT2SQL_RUNTIME_NODES),
            }
            raw_context = evolution.recent_query_context(user_id, session_id, limit=4)
            # A retry may already have written its own attempt/trace. Excluding the
            # current task keeps the semantic context stable across process restarts.
            conversation_context = {
                "scope": {"user_id": user_id, "session_id": session_id},
                "recent_query_runs": [
                    dict(item)
                    for item in raw_context.get("recent_query_runs") or ()
                    if str(item.get("task_id") or "") != query_task_id
                ][:3],
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
                    knowledge_store_path=self.knowledge_store_path,
                    vanna_index_root=self.vanna_index_root,
                    principals=effective_principals,
                    memory_snapshot_id=memory_snapshot_id,
                    policy_version=policy.version,
                    policy_artifact=policy,
                    stable_memory_provider=evolution.stable_memory,
                    result_snapshot_provider=result_snapshot,
                    checkpoint_store=checkpoint_store,
                    token_budget=self.settings.agent_token_budget,
                    time_budget=self.settings.agent_time_budget_seconds,
                    max_rows=200,
                )

            stable_engine = engine_for(active_policy_version)
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

            try:
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
                )
            except Exception as exc:
                evolution.finish_query_attempt(query_task_id, "error", str(exc))
                raise
        public = self._public_result(result, query_task_id)
        self._remember_trace(
            public,
            result,
            user_id=user_id,
            session_id=session_id,
        )
        return public
