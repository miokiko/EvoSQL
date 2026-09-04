"""Strictly bounded, role-scoped policy artifacts for Text2SQL evolution."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from .database_tools import ROLE_TOOL_PERMISSIONS
from .sql_safety import validate_sql


POLICY_CONTRACT_VERSION = "text2sql-policy-v2"
LEGACY_POLICY_CONTRACT_VERSION = "text2sql-policy-v1"
# Keep this list explicit.  ROLE_TOOL_PERMISSIONS also contains the
# non-evolvable text2sql-harness principal, which must never become a policy
# mutation target merely because it owns deterministic tools.
TEXT2SQL_SKILLS = (
    "text2sql-lead",
    "schema-grounding",
    "query-planning",
    "sql-generation",
    "text2sql-critic",
)
LEGACY_TEXT2SQL_SKILLS = (
    "text2sql-lead",
    "schema-grounding",
    "sql-strategy",
    "text2sql-critic",
)
POLICY_FIELDS = frozenset(
    {
        "contract_version",
        "prompt_fragments",
        "field_aliases",
        "value_aliases",
        "few_shot_examples",
        "tool_selection_policy",
        "budget_parameters",
    }
)
RUNTIME_ROLE_TO_SKILL = {
    "text2sql-lead": "text2sql-lead",
    "schema-grounding": "schema-grounding",
    "query-planning": "query-planning",
    "sql-generation": "sql-generation",
    "text2sql-critic": "text2sql-critic",
    # Read-only compatibility for a checkpoint created before the worker was
    # split. New policy proposals cannot target this legacy name.
    "sql-strategy": "query-planning",
}

_ROLE_POLICY_FIELDS = tuple(POLICY_FIELDS.difference({"contract_version"}))

_PROMPT_FORBIDDEN = re.compile(
    r"ignore\s+(all\s+)?(previous|prior)|bypass|disable\s+(the\s+)?(gate|safety)|"
    r"write\s+to\s+(the\s+)?database|drop\s+table|insert\s+into|update\s+.+\s+set|"
    r"password|credential|api[_ -]?key|secret",
    re.I,
)

_SQL_PROGRAM = re.compile(
    r"\bselect\b[\s\S]{0,2000}\bfrom\b|"
    r"\bselect\s+(?:all\s+|distinct\s+)?(?:[-+]?(?:\d+(?:\.\d*)?|\.\d+)|"
    r"null\b|true\b|false\b|'[^'\r\n]*'|\"[^\"\r\n]*\"|\*|\(|"
    r"[a-z_][a-z0-9_]*\s*\()|"
    r"\bvalues\s*\(\s*(?:[-+]?\d|['\"]|null\b|true\b|false\b|\()|"
    r"\b(?:insert\s+into|update\s+\S+\s+set|delete\s+from|"
    r"create\s+table|alter\s+table|drop\s+table|pragma)\b",
    re.I,
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _complete_legacy_artifact(raw: Mapping[str, Any]) -> bool:
    """Whether ``raw`` can carry its historical content-addressed version."""

    if set(raw) != POLICY_FIELDS:
        return False
    if raw.get("contract_version") != LEGACY_POLICY_CONTRACT_VERSION:
        return False
    return all(
        isinstance(raw.get(field), Mapping)
        and set(raw[field]) == set(LEGACY_TEXT2SQL_SKILLS)
        for field in _ROLE_POLICY_FIELDS
    )


def _migrate_legacy_policy(
    raw: Mapping[str, Any],
) -> tuple[Mapping[str, Any], str]:
    """Upgrade the former combined strategy slot to the two v2 policy slots.

    The migration deliberately splits fields by ownership: logical aliases go
    to planning, SQL examples go to generation, shared instructions/budgets go
    to both, and tool choices are intersected with each new role's maximum ACL.
    A complete persisted v1 artifact retains its old hash while it is being
    read so an existing evolution database can still resolve its active row.
    New serialization is always v2 and therefore gets a new hash on the next
    proposal/activation cycle.
    """

    mappings = [raw.get(field) for field in _ROLE_POLICY_FIELDS]
    has_legacy_slot = any(
        isinstance(value, Mapping) and "sql-strategy" in value for value in mappings
    )
    inferred_contract = (
        LEGACY_POLICY_CONTRACT_VERSION
        if has_legacy_slot and not raw.get("contract_version")
        else POLICY_CONTRACT_VERSION
    )
    contract = str(raw.get("contract_version") or inferred_contract)
    if contract == POLICY_CONTRACT_VERSION:
        return dict(raw), ""
    if contract != LEGACY_POLICY_CONTRACT_VERSION:
        raise ValueError("unsupported policy contract version")

    legacy_version = ""
    if _complete_legacy_artifact(raw):
        legacy_version = "policy-%s" % hashlib.sha256(
            _canonical(raw).encode("utf-8")
        ).hexdigest()[:20]

    migrated = dict(raw)
    migrated["contract_version"] = POLICY_CONTRACT_VERSION
    for field in _ROLE_POLICY_FIELDS:
        value = raw.get(field)
        if not isinstance(value, Mapping) or "sql-strategy" not in value:
            continue
        if "query-planning" in value or "sql-generation" in value:
            raise ValueError(
                "%s cannot mix legacy sql-strategy with v2 policy slots" % field
            )
        old_value = value["sql-strategy"]
        split = {key: item for key, item in value.items() if key != "sql-strategy"}
        if field in {"prompt_fragments", "budget_parameters"}:
            split["query-planning"] = old_value
            split["sql-generation"] = old_value
        elif field in {"field_aliases", "value_aliases"}:
            # Physical aliases belong exclusively to Grounding after the
            # plan-first split. Consolidate every legacy role's mapping there
            # with the former dedicated Grounding mapping taking precedence.
            source_order = (
                "text2sql-lead",
                "text2sql-critic",
                "sql-strategy",
                "schema-grounding",
            )
            if all(isinstance(value.get(role) or {}, Mapping) for role in source_order):
                merged = {}
                for role in source_order:
                    merged.update(value.get(role) or {})
                split = {skill: {} for skill in TEXT2SQL_SKILLS}
                split["schema-grounding"] = merged
            else:
                split["schema-grounding"] = old_value
        elif field == "few_shot_examples":
            # The v1 format contained SQL examples for any role. Only the new
            # SQL Generation slot may receive them at runtime.
            source_order = (
                "sql-strategy",
                "schema-grounding",
                "text2sql-lead",
                "text2sql-critic",
            )
            if all(
                isinstance(value.get(role) or (), Sequence)
                and not isinstance(value.get(role) or (), (str, bytes))
                for role in source_order
            ):
                merged_examples = []
                seen_examples = set()
                for role in source_order:
                    for example in value.get(role) or ():
                        marker = _canonical(example)
                        if marker in seen_examples or len(merged_examples) >= 8:
                            continue
                        seen_examples.add(marker)
                        merged_examples.append(example)
                split = {skill: [] for skill in TEXT2SQL_SKILLS}
                split["sql-generation"] = merged_examples
            else:
                split["sql-generation"] = old_value
        elif field == "tool_selection_policy":
            if old_value is None:
                split["query-planning"] = None
                split["sql-generation"] = None
            elif isinstance(old_value, Sequence) and not isinstance(
                old_value, (str, bytes)
            ):
                selected = {str(item) for item in old_value}
                split["query-planning"] = sorted(
                    selected.intersection(ROLE_TOOL_PERMISSIONS["query-planning"])
                )
                split["sql-generation"] = sorted(
                    selected.intersection(ROLE_TOOL_PERMISSIONS["sql-generation"])
                )
            else:
                # Preserve malformed input so the normal validator emits the
                # same bounded type error instead of silently repairing it.
                split["query-planning"] = old_value
                split["sql-generation"] = old_value
        migrated[field] = split
    return migrated, legacy_version


def _schema_columns(snapshot: Mapping[str, Any]) -> set[str]:
    return {
        "%s.%s" % (table["name"], column["name"])
        for table in snapshot["tables"]
        for column in table["columns"]
    }


def _contains_physical_schema_or_sql(
    value: str, snapshot: Mapping[str, Any]
) -> bool:
    text = str(value or "").casefold()
    identifiers = {
        str(table["name"]).casefold() for table in snapshot["tables"]
    }
    identifiers.update(
        str(column["name"]).casefold()
        for table in snapshot["tables"]
        for column in table["columns"]
    )
    if any(
        re.search(
            r"(?<![a-z0-9_])%s(?![a-z0-9_])" % re.escape(identifier),
            text,
        )
        for identifier in identifiers
    ):
        return True
    return bool(_SQL_PROGRAM.search(text))


def _role_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("%s must be keyed by Text2SQL skill" % field)
    unknown = set(value).difference(TEXT2SQL_SKILLS)
    if unknown:
        raise ValueError("%s contains unsupported skill(s): %s" % (field, ", ".join(sorted(unknown))))
    return value


@dataclass(frozen=True)
class PolicyArtifact:
    """A complete policy snapshot; candidates may change exactly one skill."""

    value: Mapping[str, Any]
    _source_version: str = ""

    @classmethod
    def baseline(cls, snapshot: Mapping[str, Any]) -> "PolicyArtifact":
        return cls.from_dict({}, snapshot)

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any], snapshot: Mapping[str, Any]
    ) -> "PolicyArtifact":
        if not isinstance(raw, Mapping):
            raise ValueError("policy artifact must be an object")
        raw, source_version = _migrate_legacy_policy(raw)
        unknown = set(raw).difference(POLICY_FIELDS)
        if unknown:
            raise ValueError("policy contains forbidden field(s): %s" % ", ".join(sorted(unknown)))
        contract = str(raw.get("contract_version") or POLICY_CONTRACT_VERSION)
        if contract != POLICY_CONTRACT_VERSION:
            raise ValueError("unsupported policy contract version")
        columns = _schema_columns(snapshot)

        prompts_in = _role_mapping(raw.get("prompt_fragments"), "prompt_fragments")
        prompts = {}
        for skill in TEXT2SQL_SKILLS:
            fragment = str(prompts_in.get(skill) or "").strip()
            if len(fragment) > 4000:
                raise ValueError("prompt fragment exceeds 4000 characters")
            if fragment and _PROMPT_FORBIDDEN.search(fragment):
                raise ValueError("prompt fragment attempts to bypass an invariant or expose secrets")
            if (
                skill == "query-planning"
                and fragment
                and not source_version
                and _contains_physical_schema_or_sql(fragment, snapshot)
            ):
                raise ValueError(
                    "query-planning prompt fragment must remain schema-blind"
                )
            prompts[skill] = fragment

        aliases_in = _role_mapping(raw.get("field_aliases"), "field_aliases")
        aliases = {}
        for skill in TEXT2SQL_SKILLS:
            role_aliases = aliases_in.get(skill) or {}
            if not isinstance(role_aliases, Mapping) or len(role_aliases) > 100:
                raise ValueError("field_aliases for %s must be an object with at most 100 entries" % skill)
            if role_aliases and skill != "schema-grounding":
                raise ValueError(
                    "physical field_aliases are owned only by schema-grounding"
                )
            normalized = {}
            for alias, target in role_aliases.items():
                alias = str(alias).strip()
                target = str(target).strip()
                if not alias or len(alias) > 100 or target not in columns:
                    raise ValueError("invalid field alias for %s: %s -> %s" % (skill, alias, target))
                normalized[alias] = target
            aliases[skill] = dict(sorted(normalized.items()))

        values_in = _role_mapping(raw.get("value_aliases"), "value_aliases")
        values = {}
        for skill in TEXT2SQL_SKILLS:
            role_values = values_in.get(skill) or {}
            if not isinstance(role_values, Mapping) or len(role_values) > 100:
                raise ValueError("value_aliases for %s must be an object with at most 100 entries" % skill)
            if role_values and skill != "schema-grounding":
                raise ValueError(
                    "physical value_aliases are owned only by schema-grounding"
                )
            normalized_values = {}
            for alias, binding in role_values.items():
                alias = str(alias).strip()
                if not alias or len(alias) > 100 or not isinstance(binding, Mapping):
                    raise ValueError("invalid value alias for %s" % skill)
                if set(binding).difference({"column", "value"}):
                    raise ValueError("value alias contains forbidden fields")
                column = str(binding.get("column") or "")
                scalar = binding.get("value")
                if column not in columns or not isinstance(scalar, (str, int, float, bool, type(None))):
                    raise ValueError("invalid value alias binding for %s" % alias)
                if isinstance(scalar, float) and not math.isfinite(scalar):
                    raise ValueError("value alias must be a finite scalar")
                if len(str(scalar)) > 500:
                    raise ValueError("value alias exceeds 500 characters")
                normalized_values[alias] = {"column": column, "value": scalar}
            values[skill] = dict(sorted(normalized_values.items()))

        examples_in = _role_mapping(raw.get("few_shot_examples"), "few_shot_examples")
        examples = {}
        for skill in TEXT2SQL_SKILLS:
            role_examples = examples_in.get(skill) or []
            if not isinstance(role_examples, Sequence) or isinstance(role_examples, (str, bytes)):
                raise ValueError("few_shot_examples for %s must be a list" % skill)
            if len(role_examples) > 8:
                raise ValueError("few_shot_examples are limited to 8 per skill")
            if role_examples and skill != "sql-generation":
                raise ValueError(
                    "SQL few_shot_examples are owned only by sql-generation"
                )
            normalized_examples = []
            for item in role_examples:
                if not isinstance(item, Mapping) or set(item).difference({"question", "sql"}):
                    raise ValueError("few-shot example contains forbidden fields")
                question = str(item.get("question") or "").strip()
                sql = str(item.get("sql") or "").strip()
                if not question or len(question) > 1000 or not sql or len(sql) > 5000:
                    raise ValueError("few-shot example has invalid question or SQL length")
                gate = validate_sql(sql, snapshot)
                if not gate.accepted:
                    raise ValueError("few-shot SQL failed deterministic safety validation: %s" % ",".join(gate.errors))
                normalized_examples.append({"question": question, "sql": sql})
            examples[skill] = normalized_examples

        tools_in = _role_mapping(raw.get("tool_selection_policy"), "tool_selection_policy")
        tools = {}
        for skill in TEXT2SQL_SKILLS:
            configured = tools_in.get(skill)
            if configured is None:
                tools[skill] = None
                continue
            if not isinstance(configured, Sequence) or isinstance(configured, (str, bytes)):
                raise ValueError("tool_selection_policy for %s must be a list" % skill)
            selected = {str(item) for item in configured}
            expanded = selected.difference(ROLE_TOOL_PERMISSIONS[skill])
            if expanded:
                raise ValueError("policy cannot expand %s tool privileges: %s" % (skill, ", ".join(sorted(expanded))))
            tools[skill] = sorted(selected)

        budgets_in = _role_mapping(raw.get("budget_parameters"), "budget_parameters")
        budgets = {}
        limits = {
            "token_budget": (512, 12000),
            "time_budget": (5, 120),
            "max_steps": (1, 8),
        }
        for skill in TEXT2SQL_SKILLS:
            configured = budgets_in.get(skill) or {}
            if not isinstance(configured, Mapping) or set(configured).difference(limits):
                raise ValueError("budget_parameters for %s contain forbidden fields" % skill)
            normalized_budget = {}
            for name, value in configured.items():
                if not isinstance(value, int) or isinstance(value, bool):
                    raise ValueError("budget values must be integers")
                number = value
                low, high = limits[name]
                if number < low or number > high:
                    raise ValueError("%s must be between %d and %d" % (name, low, high))
                normalized_budget[name] = number
            budgets[skill] = dict(sorted(normalized_budget.items()))

        return cls(
            {
                "contract_version": contract,
                "prompt_fragments": prompts,
                "field_aliases": aliases,
                "value_aliases": values,
                "few_shot_examples": examples,
                "tool_selection_policy": tools,
                "budget_parameters": budgets,
            },
            source_version,
        )

    @property
    def version(self) -> str:
        return self._source_version or "policy-%s" % hashlib.sha256(
            _canonical(self.value).encode("utf-8")
        ).hexdigest()[:20]

    @property
    def was_migrated_from_v1(self) -> bool:
        return bool(self._source_version)

    def as_dict(self) -> Mapping[str, Any]:
        return json.loads(_canonical(self.value))

    def changed_skills(self, parent: "PolicyArtifact") -> tuple[str, ...]:
        changed = []
        for skill in TEXT2SQL_SKILLS:
            if any(self.value[field][skill] != parent.value[field][skill] for field in POLICY_FIELDS if field != "contract_version"):
                changed.append(skill)
        return tuple(changed)

    def role_policy(self, runtime_role: str) -> Mapping[str, Any]:
        try:
            skill = RUNTIME_ROLE_TO_SKILL[runtime_role]
        except KeyError as exc:
            raise ValueError("unsupported Text2SQL runtime role: %s" % runtime_role) from exc
        return {
            "skill": skill,
            "prompt_fragment": self.value["prompt_fragments"][skill],
            "field_aliases": self.value["field_aliases"][skill],
            "value_aliases": self.value["value_aliases"][skill],
            "few_shot_examples": self.value["few_shot_examples"][skill],
            "allowed_tools": self.value["tool_selection_policy"][skill],
            "budget_parameters": self.value["budget_parameters"][skill],
        }


def require_single_skill_change(
    parent: PolicyArtifact, candidate: PolicyArtifact, target_skill: str
) -> None:
    if target_skill not in TEXT2SQL_SKILLS:
        raise ValueError("unsupported target skill: %s" % target_skill)
    if candidate.was_migrated_from_v1:
        raise ValueError(
            "legacy v1 policy artifacts are read-only; resubmit the migrated v2 artifact"
        )
    changed = candidate.changed_skills(parent)
    if changed != (target_skill,):
        raise ValueError("candidate must change exactly %s; changed=%s" % (target_skill, list(changed)))


def policy_version(value: Mapping[str, Any], snapshot: Mapping[str, Any]) -> str:
    return PolicyArtifact.from_dict(value, snapshot).version
