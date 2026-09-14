"""Stable data contracts for Text2SQL knowledge retrieval."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


KNOWLEDGE_TYPES = frozenset(
    {"schema", "business_glossary", "value", "relationship", "verified_example"}
)


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    source_kind: str
    knowledge_type: str
    title: str
    content: str
    database_snapshot_id: str
    source_version: str
    source_url: str = ""
    dependencies: Sequence[str] = field(default_factory=tuple)
    score: float = 0.0
    planning_content: str = ""
    knowledge_status: str = ""

    def as_dict(self) -> Mapping[str, Any]:
        value = {
            "evidence_id": self.evidence_id,
            "source_kind": self.source_kind,
            "knowledge_type": self.knowledge_type,
            "title": self.title,
            "content": self.content,
            "database_snapshot_id": self.database_snapshot_id,
            "source_version": self.source_version,
            "source_url": self.source_url,
            "dependencies": list(self.dependencies),
            "score": self.score,
        }
        if self.planning_content:
            value["planning_content"] = self.planning_content
        if self.knowledge_status:
            value["knowledge_status"] = self.knowledge_status
        return value


@dataclass(frozen=True)
class EvidencePack:
    query: str
    role: str
    database_snapshot_id: str
    wiki_index_version: str
    memory_snapshot_id: str
    policy_version: str
    evidence: Sequence[Evidence]

    def __post_init__(self) -> None:
        pins = (
            self.database_snapshot_id,
            self.wiki_index_version,
            self.memory_snapshot_id,
            self.policy_version,
        )
        if any(not value for value in pins):
            raise ValueError("all EvidencePack versions must be pinned")
        identifiers = [item.evidence_id for item in self.evidence]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("EvidencePack cannot contain duplicate evidence_id values")

    def as_dict(self) -> Mapping[str, Any]:
        return {
            "query": self.query,
            "role": self.role,
            "database_snapshot_id": self.database_snapshot_id,
            "wiki_index_version": self.wiki_index_version,
            "memory_snapshot_id": self.memory_snapshot_id,
            "policy_version": self.policy_version,
            "evidence": [item.as_dict() for item in self.evidence],
        }
