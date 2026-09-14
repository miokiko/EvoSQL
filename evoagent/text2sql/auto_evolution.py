"""Fail-closed automation for the Experience -> Policy part of EvoSQL.

The automatic path is deliberately narrower than the manual control plane.  It
may admit only Experience records whose before/after evidence can be checked by
code.  The LLM may summarize that evidence and propose a prompt, but it cannot
approve its own evidence or bypass Target Replay and the independent release
evaluation.
"""

from __future__ import annotations

from typing import Any, Mapping

from .memory_attribution import EXPERIENCE_MEMORY_CONTRACT
from .policy import TEXT2SQL_SKILLS
from .semantic_rules import generate_semantic_rule, propose_policy_from_rules


AUTOMATION_ACTOR = "text2sql-auto-evolution"


def _existing_rule_for_memory(store: Any, memory_id: str) -> Mapping[str, Any]:
    """Return the newest reusable rule for one Experience, if present."""

    rules = [
        item
        for item in store.list_semantic_rules(limit=100)
        if list(item.get("source_memory_ids") or ()) == [memory_id]
        and item.get("state") in {"candidate", "confirmed"}
    ]
    rules.sort(
        key=lambda item: (
            item.get("state") == "confirmed",
            str(item.get("created_at") or ""),
            str(item.get("rule_id") or ""),
        ),
        reverse=True,
    )
    return rules[0] if rules else {}


def _existing_policy_for_rule(store: Any, rule_id: str) -> Mapping[str, Any]:
    """Avoid duplicate Policy candidates when an automatic worker retries."""

    matches = []
    for item in store.list_policies():
        metadata = item.get("proposal_metadata")
        if not isinstance(metadata, Mapping):
            continue
        if rule_id in list(metadata.get("semantic_rule_ids") or ()):
            matches.append(item)
    return matches[-1] if matches else {}


def automatic_experience_decision(item: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return whether one immutable Experience is safe to admit automatically."""

    reasons: list[str] = []
    rule = item.get("rule")
    if not isinstance(rule, Mapping):
        rule = {}
        reasons.append("experience_rule_missing")
    if rule.get("contract") != EXPERIENCE_MEMORY_CONTRACT:
        reasons.append("experience_contract_invalid")
    if str(item.get("state") or rule.get("state") or "") not in {
        "candidate",
        "confirmed",
    }:
        reasons.append("experience_not_admissible")
    if item.get("runtime_eligible") is not False:
        reasons.append("experience_must_not_be_runtime_eligible")
    if str(rule.get("target_agent") or "") not in TEXT2SQL_SKILLS:
        reasons.append("target_agent_invalid")
    if not str(rule.get("source_task_id") or "").strip():
        reasons.append("source_task_missing")

    grade = str(rule.get("evidence_grade") or "")
    before = rule.get("before") if isinstance(rule.get("before"), Mapping) else {}
    after = rule.get("after") if isinstance(rule.get("after"), Mapping) else {}
    evidence = rule.get("evidence") if isinstance(rule.get("evidence"), Mapping) else {}

    if grade == "deterministic_plan_revision":
        if before.get("issue_present") is not True:
            reasons.append("plan_issue_not_proven")
        if after.get("issue_resolved") is not True:
            reasons.append("plan_issue_not_resolved")
        before_plan = str(before.get("worker_plan_fingerprint") or "")
        after_plan = str(after.get("worker_plan_fingerprint") or "")
        if not before_plan or not after_plan or before_plan == after_plan:
            reasons.append("plan_change_not_proven")
        if not str(after.get("approved_plan_fingerprint") or ""):
            reasons.append("approved_plan_missing")
    elif grade == "deterministic_repair":
        if before.get("gate_accepted") is not False:
            reasons.append("rejected_sql_not_proven")
        if after.get("gate_accepted") is not True:
            reasons.append("repaired_sql_not_accepted")
        if not list(before.get("gate_codes") or ()):
            reasons.append("gate_codes_missing")
        if not list(before.get("sql_fingerprints") or ()):
            reasons.append("before_sql_missing")
        if not list(after.get("sql_fingerprints") or ()):
            reasons.append("after_sql_missing")
        if not str(evidence.get("approved_plan_fingerprint") or ""):
            reasons.append("approved_plan_missing")
        if not str(evidence.get("bound_plan_fingerprint") or ""):
            reasons.append("bound_plan_missing")
    elif grade == "human_correction_with_gate":
        if evidence.get("feedback_decision") != "incorrect":
            reasons.append("negative_user_feedback_missing")
        if evidence.get("feedback_note_present") is not True:
            reasons.append("feedback_reason_missing")
        if evidence.get("corrected_sql_accepted") is not True:
            reasons.append("corrected_sql_not_gate_accepted")
        if after.get("gate_accepted") is not True or not str(
            after.get("sql_fingerprint") or ""
        ):
            reasons.append("corrected_sql_proof_missing")
    else:
        reasons.append("evidence_grade_not_automatic")

    return {
        "eligible": not reasons,
        "memory_id": str(item.get("memory_id") or rule.get("memory_id") or ""),
        "target_agent": str(rule.get("target_agent") or ""),
        "evidence_grade": grade,
        "reasons": list(dict.fromkeys(reasons)),
    }


def confirm_machine_verifiable_experience(
    store: Any,
    memory_id: str,
    *,
    actor: str = AUTOMATION_ACTOR,
) -> Mapping[str, Any]:
    """Confirm an Experience only when deterministic admission succeeds."""

    item = store.get_memory(memory_id)
    decision = automatic_experience_decision(item)
    if not decision["eligible"]:
        raise ValueError(
            "Experience is not eligible for automatic confirmation: %s"
            % ", ".join(decision["reasons"])
        )
    if item.get("state") == "confirmed":
        return item
    return store.review_experience_memory(
        memory_id,
        "confirm",
        actor,
        "Machine-verifiable before/after evidence admitted by the automatic evolution gate.",
    )


def prepare_automatic_policy_candidate(
    store: Any,
    client: Any,
    memory_id: str,
    *,
    actor: str = AUTOMATION_ACTOR,
    token_budget: int = 6000,
) -> Mapping[str, Any]:
    """Admit evidence, derive one rule, and compile one role-scoped Prompt."""

    memory = confirm_machine_verifiable_experience(
        store, memory_id, actor=actor
    )
    rule = _existing_rule_for_memory(store, memory_id)
    if not rule:
        generated = generate_semantic_rule(
            store, client, memory_id, actor, min(int(token_budget), 4000)
        )
        if generated.get("status") == "skipped":
            return {
                "status": "skipped",
                "memory_id": memory_id,
                "reason": str(
                    generated.get("reason") or "rule_generator_skipped"
                ),
            }
        rule = generated.get("rule")
        if not isinstance(rule, Mapping):
            raise ValueError("SemanticRule generator did not return a rule")
    if rule.get("state") == "candidate":
        rule = store.review_semantic_rule(
            str(rule["rule_id"]),
            "confirm",
            actor,
            "Automatically admitted; downstream Target Replay and release evaluation remain mandatory.",
        )
    if rule.get("state") != "confirmed":
        raise ValueError("SemanticRule is not confirmed")
    policy_contract = store.ensure_current_policy_contract(actor)
    existing_policy = _existing_policy_for_rule(
        store, str(rule["rule_id"])
    )
    if existing_policy:
        return {
            "status": str(existing_policy.get("status") or "candidate"),
            "memory_id": str(memory.get("memory_id") or memory_id),
            "semantic_rule_id": str(rule["rule_id"]),
            "candidate_policy_version": str(
                existing_policy.get("policy_version") or ""
            ),
            "parent_policy_version": str(
                existing_policy.get("parent_version") or ""
            ),
            "target_agent": str(existing_policy.get("target_skill") or ""),
            "reused": True,
            "policy_contract": policy_contract,
            "next_step": "resume_candidate_release_state",
        }
    proposed = propose_policy_from_rules(
        store,
        client,
        [str(rule["rule_id"])],
        actor,
        "Automatically compiled from machine-verifiable Experience evidence.",
        token_budget,
    )
    return {
        **dict(proposed),
        "status": "candidate",
        "memory_id": str(memory.get("memory_id") or memory_id),
        "semantic_rule_id": str(rule["rule_id"]),
        "policy_contract": policy_contract,
        "next_step": "target_replay_then_independent_release_evaluation",
    }
