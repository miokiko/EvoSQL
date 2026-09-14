"""Deterministic source-case replay gate for Experience-driven Policy candidates.

This module deliberately separates execution from judgement. Callers supply two
otherwise-identical runners (parent Policy and candidate Policy), while this
module validates source lineage, executes each unique source QueryTrace once per
lane, and emits a redacted, content-addressed artifact. Questions, SQL text,
result rows, prompts, and exception messages never enter that artifact.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence


TARGET_REPLAY_CONTRACT = "Text2SQLTargetReplay/v1"

_EXPERIENCE_CONTRACT = "ExperienceMemory/v1"
_POLICY_ROLES = frozenset(
    {
        "text2sql-lead",
        "schema-grounding",
        "query-planning",
        "sql-generation",
        "text2sql-critic",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.I)
_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,99}$", re.I)
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_.:/-]{0,199}$", re.I)
_SHARED_PIN_FIELDS = (
    "database_snapshot_id",
    "wiki_index_version",
    "vanna_index_version",
    "memory_snapshot_id",
)
_PUBLIC_RUNTIME_FIELDS = (
    "protocol",
    "build_version",
    "gate_implementation_version",
    "nodes",
    "plan_contracts",
    "max_candidates",
    "max_plan_revisions_per_worker",
    "max_sql_repairs",
    "token_budget",
    "time_budget",
    "max_rows",
    "timeout_ms",
)
_RESULT_FIELDS = frozenset(
    {
        "memory_id",
        "source_task_id",
        "source_revision",
        "source_question_sha256",
        "source_trace_sha256",
        "target_agent",
        "problem_code",
        "baseline_problem_present",
        "candidate_problem_present",
        "baseline_signal",
        "candidate_signal",
        "baseline_issue_codes",
        "candidate_issue_codes",
        "new_issue_codes",
        "candidate_safe",
        "passed",
        "reasons",
        "baseline_result_sha256",
        "candidate_result_sha256",
    }
)
_FAILURE_REASONS = frozenset(
    {
        "baseline_source_problem_not_reproduced",
        "baseline_source_problem_not_verifiable",
        "candidate_source_problem_still_present",
        "candidate_source_problem_not_verifiable",
        "candidate_not_safely_executable",
        "candidate_introduced_new_issue_codes",
    }
)
_SIGNAL = re.compile(
    r"^(?:"
    r"sql_generation_repairs=\d+|"
    r"revision_issue_present=(?:true|false)|"
    r"expected_correction_fingerprint_match=(?:true|false)|"
    r"sql_gate_repair_proof_unavailable|"
    r"plan_revision_proof_unavailable|"
    r"user_feedback_requires_valid_expected_sql_fingerprint|"
    r"unsupported_experience_verifier"
    r")$"
)

ReplayRunner = Callable[[str, str], Mapping[str, Any]]


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text_sha(value: Any) -> str:
    text = str(value or "").strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _safe_identifier(value: Any, *, fallback_prefix: str) -> str:
    text = str(value or "").strip()[:200]
    if text and _SAFE_ID.fullmatch(text):
        return text
    return "%s:%s" % (fallback_prefix, _text_sha(text)[:20]) if text else ""


def _experience_body(value: Mapping[str, Any]) -> Mapping[str, Any]:
    rule = value.get("rule")
    if isinstance(rule, Mapping) and rule.get("contract") == _EXPERIENCE_CONTRACT:
        return rule
    return value


def _issue_code(value: Any) -> str:
    """Return a bounded code, hashing unstructured text instead of leaking it."""

    if isinstance(value, Mapping):
        value = value.get("code") or value.get("error") or value.get("message")
    text = str(value or "").strip().casefold().split(":", 1)[0]
    if not text:
        return ""
    if _CODE.fullmatch(text):
        return text
    return "unstructured_issue.%s" % _text_sha(text)[:20]


def result_issue_codes(result: Mapping[str, Any]) -> tuple[str, ...]:
    """Collect deterministic issue identifiers without retaining messages or SQL."""

    values: list[Any] = list(_sequence(_mapping(result.get("gates")).get("errors")))
    collaboration = _mapping(result.get("collaboration"))
    values.extend(_sequence(collaboration.get("plan_approval_errors")))
    values.extend(_sequence(collaboration.get("initial_binding_conflicts")))
    values.extend(_sequence(collaboration.get("binding_conflicts")))
    requests = collaboration.get("revision_requests")
    if not _sequence(requests):
        # Compatibility with traces created before the normalized request list
        # was projected at the top level of ``collaboration``.
        requests = _mapping(collaboration.get("lead_assessment")).get(
            "revision_requests"
        )
    for request in _sequence(requests):
        if not isinstance(request, Mapping):
            continue
        values.extend(
            (
                request.get("problem_code"),
                request.get("code"),
                request.get("issue_code"),
            )
        )
        values.extend(_sequence(request.get("issue_codes")))
        values.extend(_sequence(request.get("issues")))
    for round_value in _sequence(collaboration.get("candidate_gate_rounds")):
        if not isinstance(round_value, Mapping):
            continue
        values.extend(_sequence(round_value.get("gate_issues")))
        for gate_result in _sequence(round_value.get("candidate_gate_results")):
            if isinstance(gate_result, Mapping):
                values.extend(_sequence(gate_result.get("errors")))
                values.extend(
                    _sequence(_mapping(gate_result.get("validation")).get("errors"))
                )
    return tuple(sorted({_issue_code(item) for item in values if _issue_code(item)}))


def _expected_sql_fingerprints(experience: Mapping[str, Any]) -> tuple[str, ...]:
    after = _mapping(experience.get("after"))
    values = [after.get("sql_fingerprint")]
    values.extend(_sequence(after.get("sql_fingerprints")))
    return tuple(
        dict.fromkeys(
            str(value).casefold()
            for value in values
            if _SHA256.fullmatch(str(value or "").strip())
        )
    )


def _result_sql_fingerprints(result: Mapping[str, Any]) -> tuple[str, ...]:
    """Return raw and deterministic-gate SQL fingerprints for one replay result."""

    values = [_text_sha(result.get("final_sql"))]
    gates = _mapping(result.get("gates"))
    values.extend(
        (
            _mapping(gates.get("ast")).get("fingerprint"),
            _mapping(gates.get("validation")).get("fingerprint"),
            _mapping(result.get("answer")).get("sql_fingerprint"),
        )
    )
    return tuple(
        dict.fromkeys(
            str(value).casefold()
            for value in values
            if _SHA256.fullmatch(str(value or "").strip())
        )
    )


def _plan_revision_proof(experience: Mapping[str, Any]) -> bool:
    problem_code = str(experience.get("problem_code") or "").casefold()
    before = _mapping(experience.get("before"))
    after = _mapping(experience.get("after"))
    applicability = _mapping(experience.get("applicability"))
    evidence = _mapping(experience.get("evidence"))
    proof_code = str(
        before.get("issue_code") or applicability.get("issue_code") or ""
    ).casefold()
    approved = str(
        after.get("approved_plan_fingerprint")
        or evidence.get("approved_plan_fingerprint")
        or ""
    )
    bound = str(
        after.get("bound_plan_fingerprint")
        or evidence.get("bound_plan_fingerprint")
        or ""
    )
    request_fingerprint = str(before.get("revision_request_fingerprint") or "")
    evidence_request_fingerprint = str(
        evidence.get("revision_request_fingerprint") or ""
    )
    initial_plan_fingerprint = str(before.get("worker_plan_fingerprint") or "")
    final_plan_fingerprint = str(after.get("worker_plan_fingerprint") or "")
    initial_conflicts_fingerprint = str(
        evidence.get("initial_binding_conflicts_fingerprint") or ""
    )
    return bool(
        _CODE.fullmatch(problem_code)
        and proof_code == problem_code
        and before.get("issue_present") is True
        and after.get("issue_resolved") is True
        and _SHA256.fullmatch(request_fingerprint)
        and request_fingerprint == evidence_request_fingerprint
        and _SHA256.fullmatch(initial_conflicts_fingerprint)
        and _SHA256.fullmatch(initial_plan_fingerprint)
        and _SHA256.fullmatch(final_plan_fingerprint)
        and initial_plan_fingerprint != final_plan_fingerprint
        and _SHA256.fullmatch(approved)
        and _SHA256.fullmatch(bound)
    )


def _sql_repair_proof(experience: Mapping[str, Any]) -> bool:
    before = _mapping(experience.get("before"))
    after = _mapping(experience.get("after"))
    applicability = _mapping(experience.get("applicability"))
    evidence = _mapping(experience.get("evidence"))
    approved = str(
        applicability.get("approved_plan_fingerprint")
        or evidence.get("approved_plan_fingerprint")
        or ""
    )
    gate_codes = tuple(
        code
        for code in (_issue_code(item) for item in _sequence(before.get("gate_codes")))
        if code
    )
    return bool(
        experience.get("problem_code") == "sql_gate_repair"
        and before.get("gate_accepted") is False
        and after.get("gate_accepted") is True
        and applicability.get("single_repair") is True
        and gate_codes
        and _SHA256.fullmatch(approved)
    )


def experience_has_replay_proof(value: Mapping[str, Any]) -> bool:
    """Return whether an Experience has a source-specific deterministic verifier.

    The predicate deliberately does not require ``state=confirmed`` so the Store
    can use it while deciding whether a candidate Experience is confirmable.
    Unsupported stages and incomplete proof fail closed.
    """

    if not isinstance(value, Mapping):
        return False
    experience = _experience_body(value)
    if experience.get("contract") != _EXPERIENCE_CONTRACT:
        return False
    source_stage = str(experience.get("source_stage") or "").casefold()
    problem_code = str(experience.get("problem_code") or "").casefold()
    if source_stage == "user-feedback":
        return bool(_expected_sql_fingerprints(experience))
    if source_stage == "candidate-gates" or problem_code == "sql_gate_repair":
        return _sql_repair_proof(experience)
    if source_stage == "plan-revisions" or problem_code.endswith("_plan_revision"):
        return _plan_revision_proof(experience)
    return False


def _problem_present(
    experience: Mapping[str, Any], result: Mapping[str, Any]
) -> tuple[bool | None, str]:
    """Detect the source problem using only a source-specific public signal."""

    problem_code = str(experience.get("problem_code") or "").casefold()
    source_stage = str(experience.get("source_stage") or "").casefold()
    collaboration = _mapping(result.get("collaboration"))
    issues = set(result_issue_codes(result))

    if source_stage == "candidate-gates" or problem_code == "sql_gate_repair":
        if not _sql_repair_proof(experience):
            return None, "sql_gate_repair_proof_unavailable"
        repairs = int(collaboration.get("sql_generation_repairs") or 0)
        return repairs > 0, "sql_generation_repairs=%d" % repairs

    if source_stage == "plan-revisions" or problem_code.endswith("_plan_revision"):
        if not _plan_revision_proof(experience):
            return None, "plan_revision_proof_unavailable"
        present = problem_code in issues
        return present, "revision_issue_present=%s" % str(present).lower()

    if source_stage == "user-feedback":
        expected_sql = _expected_sql_fingerprints(experience)
        if not expected_sql:
            return None, "user_feedback_requires_valid_expected_sql_fingerprint"
        actual = set(_result_sql_fingerprints(result))
        if not actual:
            return True, "expected_correction_fingerprint_match=false"
        present = not bool(actual.intersection(expected_sql))
        return (
            present,
            "expected_correction_fingerprint_match=%s" % str(not present).lower(),
        )

    # No generic semantic inference is allowed. A confirmed Experience from an
    # unsupported stage needs a new verifier before it can gate a Policy release.
    return None, "unsupported_experience_verifier"


def _failure_reasons(
    baseline_present: bool | None,
    candidate_present: bool | None,
    candidate_safe: bool,
    new_issues: Sequence[str],
) -> list[str]:
    reasons = []
    if baseline_present is not True:
        reasons.append(
            "baseline_source_problem_not_reproduced"
            if baseline_present is False
            else "baseline_source_problem_not_verifiable"
        )
    if candidate_present is not False:
        reasons.append(
            "candidate_source_problem_still_present"
            if candidate_present is True
            else "candidate_source_problem_not_verifiable"
        )
    if not candidate_safe:
        reasons.append("candidate_not_safely_executable")
    if new_issues:
        reasons.append("candidate_introduced_new_issue_codes")
    return reasons


def _signal_state(signal: str) -> bool | None:
    if signal.startswith("sql_generation_repairs="):
        return int(signal.rsplit("=", 1)[1]) > 0
    if signal.startswith("revision_issue_present="):
        return signal.endswith("=true")
    if signal.startswith("expected_correction_fingerprint_match="):
        return not signal.endswith("=true")
    return None


def build_replay_identity(
    *,
    parent_version_pins: Mapping[str, Any],
    candidate_version_pins: Mapping[str, Any],
    parent_runtime: Mapping[str, Any],
    candidate_runtime: Mapping[str, Any],
    model: Mapping[str, Any],
    principals: Sequence[str],
) -> Mapping[str, Any]:
    """Validate equal lane inputs and return their artifact-safe identity."""

    parent_pins = _mapping(parent_version_pins)
    candidate_pins = _mapping(candidate_version_pins)
    parent_policy = str(parent_pins.get("policy_version") or "")
    candidate_policy = str(candidate_pins.get("policy_version") or "")
    if not parent_policy or not candidate_policy or parent_policy == candidate_policy:
        raise ValueError(
            "target replay requires distinct parent and candidate Policies"
        )

    shared_pins = {}
    for field in _SHARED_PIN_FIELDS:
        parent_value = str(parent_pins.get(field) or "")
        candidate_value = str(candidate_pins.get(field) or "")
        if not parent_value or parent_value != candidate_value:
            raise ValueError("target replay lanes must share %s" % field)
        if field == "vanna_index_version" and parent_value.startswith("fallback:"):
            raise ValueError("target replay requires a ready pinned Vanna index")
        shared_pins[field] = _safe_identifier(
            parent_value, fallback_prefix="pin-sha256"
        )

    parent_runtime_value = dict(_mapping(parent_runtime))
    candidate_runtime_value = dict(_mapping(candidate_runtime))
    parent_runtime_value.pop("policy_source_memory_ids", None)
    candidate_runtime_value.pop("policy_source_memory_ids", None)
    if not parent_runtime_value or parent_runtime_value != candidate_runtime_value:
        raise ValueError("target replay lanes must share runtime parameters")
    if set(parent_runtime_value) != set(_PUBLIC_RUNTIME_FIELDS):
        raise ValueError("target replay runtime identity contract is incomplete")
    public_runtime = {
        field: parent_runtime_value[field]
        for field in _PUBLIC_RUNTIME_FIELDS
        if field in parent_runtime_value
    }

    provider = str(_mapping(model).get("provider") or "").strip()
    model_name = str(_mapping(model).get("model") or "").strip()
    temperature = _mapping(model).get("temperature")
    if not provider or not model_name or temperature not in (0, 0.0):
        raise ValueError("target replay requires a pinned deterministic model identity")
    normalized_principals = tuple(
        sorted({str(value).strip() for value in principals if str(value).strip()})
    )
    if not normalized_principals:
        raise ValueError("target replay requires at least one principal")

    identity = {
        "policy_versions": {
            "parent": _safe_identifier(parent_policy, fallback_prefix="policy-sha256"),
            "candidate": _safe_identifier(
                candidate_policy, fallback_prefix="policy-sha256"
            ),
        },
        "shared_version_pins": shared_pins,
        "model": {
            "provider": _safe_identifier(provider, fallback_prefix="provider-sha256"),
            "model": _safe_identifier(model_name, fallback_prefix="model-sha256"),
            "temperature": 0,
        },
        "runtime": public_runtime,
        "runtime_sha256": _sha(public_runtime),
        "principal_count": len(normalized_principals),
        "principals_sha256": _sha(normalized_principals),
    }
    return {**identity, "identity_sha256": _sha(identity)}


def _validated_replay_identity(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Reject extra identity fields so an artifact cannot smuggle raw payloads."""

    allowed = {
        "policy_versions",
        "shared_version_pins",
        "model",
        "runtime",
        "runtime_sha256",
        "principal_count",
        "principals_sha256",
        "identity_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != allowed:
        raise ValueError("target replay identity contains forbidden or missing fields")
    payload = {key: item for key, item in value.items() if key != "identity_sha256"}
    if value.get("identity_sha256") != _sha(payload):
        raise ValueError("target replay identity hash mismatch")

    policy_versions = value.get("policy_versions")
    if (
        not isinstance(policy_versions, Mapping)
        or set(policy_versions) != {"parent", "candidate"}
        or not _SAFE_ID.fullmatch(str(policy_versions.get("parent") or ""))
        or not _SAFE_ID.fullmatch(str(policy_versions.get("candidate") or ""))
        or policy_versions.get("parent") == policy_versions.get("candidate")
    ):
        raise ValueError("target replay identity Policy pins are invalid")

    pins = value.get("shared_version_pins")
    if not isinstance(pins, Mapping) or set(pins) != set(_SHARED_PIN_FIELDS):
        raise ValueError("target replay shared version pins are incomplete")
    if any(
        not _SAFE_ID.fullmatch(str(pins.get(field) or ""))
        for field in _SHARED_PIN_FIELDS
    ):
        raise ValueError("target replay shared version pin is unsafe")
    if str(pins.get("vanna_index_version") or "").startswith("fallback:"):
        raise ValueError("target replay requires a ready pinned Vanna index")

    model = value.get("model")
    if not isinstance(model, Mapping) or set(model) != {
        "provider",
        "model",
        "temperature",
    }:
        raise ValueError("target replay model identity is incomplete")
    if (
        not _SAFE_ID.fullmatch(str(model.get("provider") or ""))
        or not _SAFE_ID.fullmatch(str(model.get("model") or ""))
        or type(model.get("temperature")) not in {int, float}
        or model.get("temperature") != 0
    ):
        raise ValueError("target replay model identity is invalid")

    runtime = value.get("runtime")
    if not isinstance(runtime, Mapping) or set(runtime) != set(_PUBLIC_RUNTIME_FIELDS):
        raise ValueError("target replay runtime identity is incomplete")
    for field in ("protocol", "build_version", "gate_implementation_version"):
        if not _SAFE_ID.fullmatch(str(runtime.get(field) or "")):
            raise ValueError("target replay runtime identifier is invalid")
    for field in ("nodes", "plan_contracts"):
        items = runtime.get(field)
        if (
            not isinstance(items, list)
            or not items
            or any(not _SAFE_ID.fullmatch(str(item or "")) for item in items)
        ):
            raise ValueError("target replay runtime sequence is invalid")
    for field in (
        "max_candidates",
        "max_plan_revisions_per_worker",
        "max_sql_repairs",
        "token_budget",
        "time_budget",
        "max_rows",
        "timeout_ms",
    ):
        if type(runtime.get(field)) is not int or runtime[field] <= 0:
            raise ValueError("target replay runtime limit is invalid")
    if value.get("runtime_sha256") != _sha(runtime):
        raise ValueError("target replay runtime hash mismatch")
    if (
        type(value.get("principal_count")) is not int
        or value["principal_count"] <= 0
        or not _SHA256.fullmatch(str(value.get("principals_sha256") or ""))
    ):
        raise ValueError("target replay principal identity is invalid")
    return json.loads(_canonical(value))


def _confirmed_experience(wrapper: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    if not isinstance(wrapper, Mapping):
        raise ValueError("target replay Experience must be an object")
    experience = _experience_body(wrapper)
    memory_id = str(wrapper.get("memory_id") or experience.get("memory_id") or "")
    state = str(wrapper.get("state") or experience.get("state") or "")
    target_agent = str(experience.get("target_agent") or "")
    source_revision = experience.get("source_revision")
    if (
        experience.get("contract") != _EXPERIENCE_CONTRACT
        or state != "confirmed"
        or not memory_id.startswith("memory-")
        or not _SAFE_ID.fullmatch(memory_id)
        or target_agent not in _POLICY_ROLES
        or type(source_revision) is not int
        or source_revision <= 0
    ):
        raise ValueError("target replay requires confirmed ExperienceMemory/v1")
    return memory_id, experience


def _source_case(
    experience: Mapping[str, Any],
    trace: Mapping[str, Any],
    replay_identity: Mapping[str, Any],
) -> tuple[str, str, Mapping[str, Any]]:
    source_task_id = str(experience.get("source_task_id") or "").strip()
    if not source_task_id or str(trace.get("task_id") or "") != source_task_id:
        raise ValueError("Experience source QueryTrace is missing or mismatched")
    source_revision = experience.get("source_revision")
    trace_revision = trace.get("source_revision")
    if (
        type(source_revision) is not int
        or source_revision <= 0
        or type(trace_revision) is not int
        or trace_revision != source_revision
    ):
        raise ValueError("Experience source QueryTrace revision is missing or mismatched")
    if str(trace.get("query_type") or "DATA_QUERY") != "DATA_QUERY":
        raise ValueError("target replay supports source DATA_QUERY traces only")
    question = str(
        trace.get("standalone_question") or trace.get("question") or ""
    ).strip()
    if not question or len(question) > 2_000:
        raise ValueError("source QueryTrace requires a bounded standalone question")

    trace_pins = _mapping(trace.get("version_pins"))
    evidence_pins = _mapping(_mapping(experience.get("evidence")).get("version_pins"))
    expected = _mapping(replay_identity.get("shared_version_pins"))
    for field in ("database_snapshot_id", "wiki_index_version", "vanna_index_version"):
        trace_value = str(trace_pins.get(field) or "")
        evidence_value = str(evidence_pins.get(field) or "")
        expected_value = str(expected.get(field) or "")
        if not trace_value or trace_value != expected_value:
            raise ValueError(
                "source QueryTrace %s cannot be replayed by this runtime" % field
            )
        if evidence_value and evidence_value != trace_value:
            raise ValueError("Experience evidence and QueryTrace version pins differ")

    metadata = {
        "source_task_id": _safe_identifier(
            source_task_id, fallback_prefix="task-sha256"
        ),
        "source_revision": source_revision,
        "source_question_sha256": _text_sha(question),
        "source_trace_sha256": _sha(trace),
    }
    return "%s@revision:%d" % (source_task_id, source_revision), question, metadata


def _safe_run(
    runner: ReplayRunner, question: str, task_id: str, lane: str
) -> Mapping[str, Any]:
    try:
        value = runner(question, task_id)
        if not isinstance(value, Mapping):
            raise TypeError("runner result is not an object")
        return value
    except Exception:
        # Exception messages can contain provider payloads, SQL, or credentials.
        # Preserve only a deterministic public failure code in the artifact.
        return {
            "status": "failed",
            "gates": {
                "accepted": False,
                "errors": ["target_replay_%s_runtime_failure" % lane],
            },
            "collaboration": {},
        }


def evaluate_target_replay(
    experiences: Sequence[Mapping[str, Any]],
    baseline_results: Mapping[str, Mapping[str, Any]],
    candidate_results: Mapping[str, Mapping[str, Any]],
    *,
    parent_policy_version: str,
    candidate_policy_version: str,
    replay_identity: Mapping[str, Any],
    source_metadata: Mapping[str, Mapping[str, Any]] | None = None,
) -> Mapping[str, Any]:
    """Build a fail-closed, hashed replay artifact for all source Experiences."""

    if not experiences:
        raise ValueError("target replay requires at least one Experience")
    if not parent_policy_version or not candidate_policy_version:
        raise ValueError("target replay requires parent and candidate Policy versions")
    if parent_policy_version == candidate_policy_version:
        raise ValueError("target replay parent and candidate must differ")
    replay_identity = _validated_replay_identity(replay_identity)
    identity_policies = _mapping(replay_identity.get("policy_versions"))
    if (
        identity_policies.get("parent") != parent_policy_version
        or identity_policies.get("candidate") != candidate_policy_version
    ):
        raise ValueError("target replay Policy pins do not match replay identity")

    rows = []
    seen = set()
    metadata_by_id = _mapping(source_metadata)
    for wrapper in experiences:
        memory_id, experience = _confirmed_experience(wrapper)
        if memory_id in seen:
            raise ValueError("target replay Experience ids must be unique")
        seen.add(memory_id)
        baseline = _mapping(baseline_results.get(memory_id))
        candidate = _mapping(candidate_results.get(memory_id))
        if not baseline or not candidate:
            raise ValueError("target replay is missing a baseline or candidate result")
        baseline_present, baseline_signal = _problem_present(experience, baseline)
        candidate_present, candidate_signal = _problem_present(experience, candidate)
        baseline_issues = set(result_issue_codes(baseline))
        candidate_issues = set(result_issue_codes(candidate))
        new_issues = sorted(candidate_issues.difference(baseline_issues))
        candidate_gates = _mapping(candidate.get("gates"))
        candidate_safe = bool(
            candidate.get("status") == "success"
            and candidate_gates.get("accepted") is True
            and not candidate_gates.get("errors")
        )
        reasons = _failure_reasons(
            baseline_present,
            candidate_present,
            candidate_safe,
            new_issues,
        )
        source = _mapping(metadata_by_id.get(memory_id))
        experience_source_task_id = str(
            experience.get("source_task_id") or ""
        ).strip()
        experience_source_revision = experience.get("source_revision")
        if source.get("source_task_id") and str(
            source.get("source_task_id")
        ) != experience_source_task_id:
            raise ValueError("target replay source task metadata mismatch")
        if source.get("source_revision") is not None and source.get(
            "source_revision"
        ) != experience_source_revision:
            raise ValueError("target replay source revision metadata mismatch")
        source_task_id = _safe_identifier(
            source.get("source_task_id") or experience_source_task_id,
            fallback_prefix="task-sha256",
        )
        source_revision = experience_source_revision
        source_question_sha256 = str(source.get("source_question_sha256") or "")
        source_trace_sha256 = str(source.get("source_trace_sha256") or "")
        row = {
            "memory_id": memory_id,
            "source_task_id": source_task_id,
            "source_revision": (
                source_revision
                if type(source_revision) is int and source_revision > 0
                else 0
            ),
            "source_question_sha256": (
                source_question_sha256
                if _SHA256.fullmatch(source_question_sha256)
                else ""
            ),
            "source_trace_sha256": (
                source_trace_sha256 if _SHA256.fullmatch(source_trace_sha256) else ""
            ),
            "target_agent": str(experience.get("target_agent") or "")[:100],
            "problem_code": _issue_code(experience.get("problem_code")),
            "baseline_problem_present": baseline_present,
            "candidate_problem_present": candidate_present,
            "baseline_signal": baseline_signal[:300],
            "candidate_signal": candidate_signal[:300],
            "baseline_issue_codes": sorted(baseline_issues),
            "candidate_issue_codes": sorted(candidate_issues),
            "new_issue_codes": new_issues,
            "candidate_safe": candidate_safe,
            "passed": not reasons,
            "reasons": reasons,
            "baseline_result_sha256": _sha(
                {
                    "status": baseline.get("status"),
                    "final_sql_sha256": _text_sha(baseline.get("final_sql")),
                    "gates_sha256": _sha(baseline.get("gates")),
                    "issues": sorted(baseline_issues),
                }
            ),
            "candidate_result_sha256": _sha(
                {
                    "status": candidate.get("status"),
                    "final_sql_sha256": _text_sha(candidate.get("final_sql")),
                    "gates_sha256": _sha(candidate.get("gates")),
                    "issues": sorted(candidate_issues),
                }
            ),
        }
        rows.append(row)

    passed_count = sum(bool(row["passed"]) for row in rows)
    artifact = {
        "contract": TARGET_REPLAY_CONTRACT,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "parent_policy_version": _safe_identifier(
            parent_policy_version, fallback_prefix="policy-sha256"
        ),
        "candidate_policy_version": _safe_identifier(
            candidate_policy_version, fallback_prefix="policy-sha256"
        ),
        "memory_ids": sorted(seen),
        "replay_identity": dict(replay_identity),
        "status": "passed" if passed_count == len(rows) else "failed",
        "summary": {
            "source_experience_count": len(rows),
            "passed_count": passed_count,
            "failed_count": len(rows) - passed_count,
        },
        "results": rows,
    }
    return {**artifact, "artifact_sha256": _sha(artifact)}


def run_target_replay(
    experiences: Sequence[Mapping[str, Any]],
    query_traces: Mapping[Any, Mapping[str, Any]],
    parent_runner: ReplayRunner,
    candidate_runner: ReplayRunner,
    *,
    parent_policy_version: str,
    candidate_policy_version: str,
    replay_identity: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Execute each unique source trace once per lane and evaluate all Experiences."""

    if len(experiences) > 50:
        raise ValueError("target replay is limited to 50 Experiences")
    baseline_by_task: dict[str, Mapping[str, Any]] = {}
    candidate_by_task: dict[str, Mapping[str, Any]] = {}
    baseline_results: dict[str, Mapping[str, Any]] = {}
    candidate_results: dict[str, Mapping[str, Any]] = {}
    source_metadata: dict[str, Mapping[str, Any]] = {}
    checked: list[tuple[str, Mapping[str, Any], str, str]] = []

    for wrapper in experiences:
        memory_id, experience = _confirmed_experience(wrapper)
        source_task_id = str(experience.get("source_task_id") or "")
        source_revision = experience.get("source_revision")
        trace: Mapping[str, Any] = {}
        # A memory-id key is unambiguous and is the preferred Store/CLI handoff.
        # Tuple/composite keys support callers that materialize traces per
        # ``task_id + source_revision``. The task-only fallback remains safe
        # because ``_source_case`` verifies the exact revision below.
        for key in (
            memory_id,
            (source_task_id, source_revision),
            "%s@revision:%s" % (source_task_id, source_revision),
            source_task_id,
        ):
            candidate_trace = _mapping(query_traces.get(key))
            if candidate_trace:
                trace = candidate_trace
                break
        task_key, question, metadata = _source_case(experience, trace, replay_identity)
        checked.append((memory_id, experience, task_key, question))
        source_metadata[memory_id] = metadata

    candidate_key = _text_sha(candidate_policy_version)[:16]
    for memory_id, _experience, source_case_key, question in checked:
        case_key = _text_sha(source_case_key)[:16]
        if source_case_key not in baseline_by_task:
            baseline_by_task[source_case_key] = _safe_run(
                parent_runner,
                question,
                "target-replay:%s:%s:parent" % (candidate_key, case_key),
                "parent",
            )
            candidate_by_task[source_case_key] = _safe_run(
                candidate_runner,
                question,
                "target-replay:%s:%s:candidate" % (candidate_key, case_key),
                "candidate",
            )
        baseline_results[memory_id] = baseline_by_task[source_case_key]
        candidate_results[memory_id] = candidate_by_task[source_case_key]

    return evaluate_target_replay(
        experiences,
        baseline_results,
        candidate_results,
        parent_policy_version=parent_policy_version,
        candidate_policy_version=candidate_policy_version,
        replay_identity=replay_identity,
        source_metadata=source_metadata,
    )


def validate_target_replay_artifact(
    value: Mapping[str, Any],
    *,
    candidate_policy_version: str = "",
) -> Mapping[str, Any]:
    """Validate artifact integrity before persistence or release-gate use."""

    if (
        not isinstance(value, Mapping)
        or value.get("contract") != TARGET_REPLAY_CONTRACT
    ):
        raise ValueError("unsupported target replay artifact contract")
    allowed = {
        "contract",
        "created_at",
        "parent_policy_version",
        "candidate_policy_version",
        "memory_ids",
        "replay_identity",
        "status",
        "summary",
        "results",
        "artifact_sha256",
    }
    if set(value).difference(allowed):
        raise ValueError("target replay artifact contains forbidden fields")
    if (
        candidate_policy_version
        and value.get("candidate_policy_version") != candidate_policy_version
    ):
        raise ValueError("target replay artifact is bound to another Policy")
    payload = {key: item for key, item in value.items() if key != "artifact_sha256"}
    if value.get("artifact_sha256") != _sha(payload):
        raise ValueError("target replay artifact hash mismatch")
    if (
        not _SAFE_ID.fullmatch(str(value.get("parent_policy_version") or ""))
        or not _SAFE_ID.fullmatch(str(value.get("candidate_policy_version") or ""))
        or value.get("parent_policy_version") == value.get("candidate_policy_version")
    ):
        raise ValueError("target replay Policy lineage is invalid")
    try:
        datetime.fromisoformat(str(value.get("created_at") or ""))
    except ValueError as exc:
        raise ValueError("target replay creation timestamp is invalid") from exc
    replay_identity = _validated_replay_identity(_mapping(value.get("replay_identity")))
    identity_policies = _mapping(replay_identity.get("policy_versions"))
    if identity_policies.get("parent") != value.get(
        "parent_policy_version"
    ) or identity_policies.get("candidate") != value.get("candidate_policy_version"):
        raise ValueError("target replay Policy pins do not match artifact lineage")
    rows = value.get("results")
    memory_ids = value.get("memory_ids")
    if (
        value.get("status") not in {"passed", "failed"}
        or not isinstance(rows, list)
        or not rows
        or not isinstance(memory_ids, list)
        or sorted(str(row.get("memory_id") or "") for row in rows) != sorted(memory_ids)
    ):
        raise ValueError("target replay artifact contents are inconsistent")
    if len(set(memory_ids)) != len(memory_ids) or any(
        not str(memory_id).startswith("memory-")
        or not _SAFE_ID.fullmatch(str(memory_id))
        for memory_id in memory_ids
    ):
        raise ValueError("target replay Experience ids are invalid")
    if memory_ids != sorted(memory_ids):
        raise ValueError("target replay Experience ids must be canonical")
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != _RESULT_FIELDS:
            raise ValueError(
                "target replay result contains forbidden or missing fields"
            )
        if (
            row.get("memory_id") not in memory_ids
            or row.get("target_agent") not in _POLICY_ROLES
            or not _CODE.fullmatch(str(row.get("problem_code") or ""))
            or (
                row.get("baseline_problem_present") is not None
                and type(row.get("baseline_problem_present")) is not bool
            )
            or (
                row.get("candidate_problem_present") is not None
                and type(row.get("candidate_problem_present")) is not bool
            )
            or type(row.get("candidate_safe")) is not bool
            or type(row.get("passed")) is not bool
            or not _SIGNAL.fullmatch(str(row.get("baseline_signal") or ""))
            or not _SIGNAL.fullmatch(str(row.get("candidate_signal") or ""))
        ):
            raise ValueError("target replay result contract is invalid")
        if row.get("source_task_id") and not _SAFE_ID.fullmatch(
            str(row.get("source_task_id"))
        ):
            raise ValueError("target replay source task identity is invalid")
        if type(row.get("source_revision")) is not int or row["source_revision"] <= 0:
            raise ValueError("target replay source revision is invalid")
        for field in (
            "source_question_sha256",
            "source_trace_sha256",
            "baseline_result_sha256",
            "candidate_result_sha256",
        ):
            if row.get(field) and not _SHA256.fullmatch(str(row.get(field))):
                raise ValueError("target replay result fingerprint is invalid")
        for field in (
            "baseline_issue_codes",
            "candidate_issue_codes",
            "new_issue_codes",
        ):
            codes = row.get(field)
            if not isinstance(codes, list) or any(
                not _CODE.fullmatch(str(code or "")) for code in codes
            ):
                raise ValueError("target replay result issue codes are invalid")
            if codes != sorted(set(codes)):
                raise ValueError("target replay result issue codes are not canonical")
        reasons = row.get("reasons")
        if not isinstance(reasons, list) or any(
            reason not in _FAILURE_REASONS for reason in reasons
        ):
            raise ValueError("target replay result reasons are invalid")
        if row.get("passed") is not (not reasons):
            raise ValueError("target replay result pass state is inconsistent")
        expected_new = sorted(
            set(row["candidate_issue_codes"]).difference(row["baseline_issue_codes"])
        )
        if row["new_issue_codes"] != expected_new:
            raise ValueError("target replay result issue delta is inconsistent")
        if row["baseline_problem_present"] is not _signal_state(
            str(row["baseline_signal"])
        ) or row["candidate_problem_present"] is not _signal_state(
            str(row["candidate_signal"])
        ):
            raise ValueError("target replay result signal is inconsistent")
        expected_reasons = _failure_reasons(
            row["baseline_problem_present"],
            row["candidate_problem_present"],
            row["candidate_safe"],
            row["new_issue_codes"],
        )
        if reasons != expected_reasons:
            raise ValueError("target replay result reasons are inconsistent")
    summary = value.get("summary")
    if not isinstance(summary, Mapping) or set(summary) != {
        "source_experience_count",
        "passed_count",
        "failed_count",
    }:
        raise ValueError("target replay summary is invalid")
    passed_count = sum(row.get("passed") is True for row in rows)
    if (
        any(
            type(summary.get(field)) is not int
            for field in (
                "source_experience_count",
                "passed_count",
                "failed_count",
            )
        )
        or summary.get("source_experience_count") != len(rows)
        or summary.get("passed_count") != passed_count
        or summary.get("failed_count") != len(rows) - passed_count
    ):
        raise ValueError("target replay summary is inconsistent")
    expected_status = (
        "passed" if all(row.get("passed") is True for row in rows) else "failed"
    )
    if value.get("status") != expected_status:
        raise ValueError("target replay artifact status is inconsistent")
    return json.loads(_canonical(value))


__all__ = [
    "TARGET_REPLAY_CONTRACT",
    "build_replay_identity",
    "experience_has_replay_proof",
    "evaluate_target_replay",
    "result_issue_codes",
    "run_target_replay",
    "validate_target_replay_artifact",
]
