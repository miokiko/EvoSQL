"""User clarification contracts and deterministic, earliest-stage diagnostics."""

from typing import Any, Mapping, Sequence


CLARIFICATION_INSTRUCTION = """
If essential user intent is ambiguous or a business definition cannot be resolved
from the supplied evidence, return action=final with clarification instead of a
plan or approval: {"clarification":{"reason_code":"ambiguous_intent|missing_business_definition",
"questions":["one concrete question for the user"],"missing_concepts":["concept"]}}.
Ask at most three short questions in the user's language. Missing database
evidence, unsupported query features, and output-format errors are not user
ambiguity; report those gaps through the normal plan/notes contract. Never ask
the user to approve SQL, bypass a gate, or invent a database fact.
Never ask the user for physical database table or column names. Resolve those
through evidence and Schema Grounding. At routing, missing business definitions
must first reach evidence orchestration; no retrieval has happened yet.
"""


def defer_routing_clarification(clarification: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
    """Resolve knowledge gaps after retrieval; retain genuinely ambiguous intent."""
    return bool(clarification) and (
        clarification.get("reason_code") == "missing_business_definition"
        or bool(context.get("clarification_continuation"))
    )


def parse_clarification(raw: Mapping[str, Any], role: str, stage: str) -> dict:
    value = raw.get("clarification")
    if value is None or value == {}:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("invalid_clarification_contract: expected an object")
    reason = value.get("reason_code")
    if not isinstance(reason, str) or reason not in {"ambiguous_intent", "missing_business_definition"}:
        raise ValueError("invalid_clarification_contract: unsupported reason_code")

    def strings(key: str, minimum: int, maximum: int, length: int) -> list[str]:
        values = value.get(key, [])
        if not isinstance(values, (list, tuple)) or not minimum <= len(values) <= maximum:
            raise ValueError("invalid_clarification_contract: invalid %s" % key)
        if any(not isinstance(item, str) or not item.strip() or len(item) > length
               for item in values):
            raise ValueError("invalid_clarification_contract: invalid %s item" % key)
        return list(dict.fromkeys(item.strip() for item in values))

    return {
        "contract": "ClarificationRequest/v1",
        "reason_code": reason,
        "questions": strings("questions", 1, 3, 500),
        "missing_concepts": strings("missing_concepts", 0, 10, 200),
        "source_role": role,
        "stage": stage,
    }


def worker_clarification(workers: Sequence[Mapping[str, Any]], stage: str) -> dict:
    requests = [item["clarification"] for item in workers if item.get("clarification")]
    if not requests:
        return {}
    return {
        **requests[0],
        "stage": stage,
        "questions": list(dict.fromkeys(q for item in requests for q in item["questions"]))[:3],
        "missing_concepts": list(dict.fromkeys(
            c for item in requests for c in item["missing_concepts"]
        ))[:10],
        "source_roles": list(dict.fromkeys(item["source_role"] for item in requests)),
    }


def clarification_response(clarification: Mapping[str, Any]) -> dict:
    return {
        "status": "needs_clarification",
        "selected_candidate": {},
        "gates": {"accepted": False, "mode": "clarification", "errors": []},
        "execution_result": {
            "columns": [], "rows": [], "row_count": 0, "truncated": False,
            "summary_text": "请补充以下信息：\n" + "\n".join(clarification["questions"]),
        },
    }


def clarification_continuation(
    trace: Mapping[str, Any], task_id: str, user_id: str, session_id: str, answer: str
) -> tuple[str, dict]:
    """Only explicit, scoped user text becomes evidence for a fresh query."""
    if (trace.get("task_id") != task_id or trace.get("user_id") != user_id
            or trace.get("session_id") != session_id
            or trace.get("status") != "needs_clarification"):
        raise ValueError("澄清请求不存在或不属于当前用户和会话")
    request = (trace.get("collaboration") or {}).get("clarification") or {}
    validated = parse_clarification({"clarification": request}, "text2sql-harness", "routing")
    original = str(trace.get("question") or "").strip()
    if not original or not validated or not answer.strip():
        raise ValueError("澄清请求缺少原问题或补充信息")
    combined = "%s\n\n用户补充：%s" % (original, answer.strip())
    if len(combined) > 2000:
        raise ValueError("原问题与补充信息合计超过 2000 字，请重新提交完整问题")
    return combined, {"task_id": task_id, "questions": validated["questions"], "answer": answer.strip()}


def diagnose_result(result: Mapping[str, Any]) -> dict:
    """Find the earliest causal failure; downstream skipped work is not failure."""
    if result.get("status") == "success":
        return {}
    collaboration = result.get("collaboration") or {}
    clarification = result.get("clarification") or collaboration.get("clarification") or {}

    def diagnostic(stage, category, code, message, action, *, details=(), retryable=False):
        return {
            "contract": "QueryDiagnostic/v1", "stage": stage,
            "category": category, "code": code, "message": message,
            "suggested_action": action, "retryable": retryable,
            "related_codes": list(dict.fromkeys(str(item) for item in details)),
        }

    if result.get("status") == "needs_clarification" and clarification:
        return diagnostic(clarification["stage"], "intent_ambiguity",
                          clarification["reason_code"], "需要补充查询口径或意图", "answer_clarification")
    route_errors = collaboration.get("route_gate_errors") or ()
    if route_errors:
        return diagnostic("routing", "context_error", route_errors[0],
                          "引用的历史查询未通过认证", "submit_complete_question", details=route_errors)
    workers = collaboration.get("worker_results") or ()
    failed = [item for item in workers if item.get("status") == "failed" or item.get("error")]
    if failed:
        error = str(failed[0].get("error") or "worker_failed")
        if any(marker in error for marker in ("supports WHERE filters only", "outside v1", "unsupported_query")):
            category, action = "unsupported_query", "rephrase_supported_query"
        elif failed[0].get("error_type") in {"RuntimeBudgetExceeded", "TimeoutError"}:
            category, action = "budget_exhausted", "review_budget"
        else:
            category, action = "worker_error", "inspect_worker_output"
        return diagnostic("planning_workers", category, failed[0].get("worker", "worker") + "_failed",
                          "计划角色未能完成输出", action, details=[error])
    conflicts = collaboration.get("binding_conflicts") or ()
    if conflicts:
        codes = [item.get("code", "binding_conflict") for item in conflicts]
        if "unsupported_query_contract" in codes and any(
            marker in str(item.get("message") or "")
            for item in conflicts for marker in ("outside v1", "supports WHERE filters only", "not supported")
        ):
            category, action = "unsupported_query", "inspect_query_contract"
        elif any(str(code).startswith("ambiguous_") for code in codes):
            category, action = "binding_ambiguity", "inspect_concept_bindings"
        elif any(str(code).startswith("missing_") for code in codes):
            category, action = "evidence_gap", "supply_grounding_evidence"
        else:
            category, action = "contract_error", "repair_plan_contract"
        return diagnostic("plan_binding", category, codes[0], "查询计划未能完成绑定", action, details=codes)
    approval = collaboration.get("plan_approval_errors") or ()
    if approval:
        category = "contract_error" if any("contract" in str(code) for code in approval) else "plan_rejection"
        return diagnostic("plan_approval", category, approval[0], "查询计划未获批准",
                          "inspect_plan_review", details=approval)
    generation = collaboration.get("sql_generation_result") or {}
    if generation.get("status") == "failed" or generation.get("error"):
        return diagnostic("sql_generation", "generation_error", "sql_generation_failure",
                          "SQL 候选生成失败", "inspect_generation_output")
    rounds = collaboration.get("candidate_gate_rounds") or ()
    latest = rounds[-1] if rounds else {}
    issues = latest.get("gate_issues") or ()
    if issues and not latest.get("accepted_candidates"):
        codes = [item.get("code", "candidate_gate_rejected") for item in issues]
        return diagnostic("candidate_gates", "candidate_rejection", codes[0],
                          "SQL 候选未通过校验", "inspect_candidate_gates", details=codes)
    critic = collaboration.get("critic_result") or {}
    errors = (result.get("gates") or {}).get("errors") or ()
    if critic.get("runtime_error") or "critic_runtime_failure" in errors:
        return diagnostic("critic", "review_error", "critic_runtime_failure",
                          "独立审查未能完成", "inspect_critic_output")
    if "critic_rejected_all_candidates" in errors or "critic_rejected_candidate" in errors:
        return diagnostic("critic", "semantic_rejection", "critic_rejection",
                          "候选未通过语义审查", "inspect_semantic_objections")
    if "invalid_final_candidate_index" in errors:
        return diagnostic("final_selection", "contract_error", "invalid_final_candidate_index",
                          "最终候选选择无效", "inspect_selection_output")
    if result.get("status") == "needs_new_query":
        return diagnostic("cached_result", "context_error", "cached_result_insufficient",
                          "历史结果无法回答当前问题", "submit_complete_question")
    return diagnostic("final_gates", "execution_blocked", errors[0] if errors else "unknown_failure",
                      "查询未执行，请查看运行记录", "inspect_trace", details=errors)
