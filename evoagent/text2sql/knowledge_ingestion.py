"""Validation and chunking for untrusted Wiki knowledge."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .models import KNOWLEDGE_TYPES
from .wiki_connector import WikiACL, WikiPage


_INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions?", re.IGNORECASE),
    re.compile(r"system\s+prompt", re.IGNORECASE),
    re.compile(r"you\s+are\s+(?:chatgpt|an?\s+assistant)", re.IGNORECASE),
    re.compile(r"忽略.{0,12}(?:指令|提示词|规则)"),
    re.compile(r"(?:系统提示词|开发者指令|执行以下指令)"),
)
_EXPLICIT_COLUMN = re.compile(r"\b(t_[A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\b")
_EXPLICIT_TABLE = re.compile(r"\b(t_[A-Za-z0-9_]+)\b")


@dataclass(frozen=True)
class KnowledgeChunk:
    title: str
    content: str
    knowledge_type: str
    dependencies: Sequence[str]
    content_sha256: str


@dataclass(frozen=True)
class PageValidation:
    errors: Sequence[str]
    chunks: Sequence[KnowledgeChunk]


def _schema_lookup(snapshot: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    tables = {table["name"] for table in snapshot["tables"]}
    columns = {
        "%s.%s" % (table["name"], column["name"])
        for table in snapshot["tables"]
        for column in table["columns"]
    }
    return tables, columns


def _chunks(title: str, content: str) -> list[tuple[str, str]]:
    chunks: list[tuple[str, str]] = []
    current_title = title
    current: list[str] = []
    for line in content.splitlines():
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if heading:
            if any(item.strip() for item in current):
                chunks.append((current_title, "\n".join(current).strip()))
            current_title = heading.group(2).strip()
            current = []
        else:
            current.append(line)
    if any(item.strip() for item in current):
        chunks.append((current_title, "\n".join(current).strip()))
    return chunks or [(title, content.strip())]


def validate_and_chunk_page(
    page: WikiPage,
    acl: WikiACL,
    database_snapshot: Mapping[str, Any],
) -> PageValidation:
    errors: list[str] = []
    metadata = page.metadata
    knowledge_type = str(metadata.get("knowledge_type") or "")
    if knowledge_type not in KNOWLEDGE_TYPES:
        errors.append("invalid_or_missing_knowledge_type")
    chunk_type = knowledge_type if knowledge_type in KNOWLEDGE_TYPES else "business_glossary"
    if not page.owner_id:
        errors.append("missing_owner")
    if not acl.principals:
        errors.append("missing_acl")
    pinned_snapshot = str(metadata.get("database_snapshot_id") or "")
    if pinned_snapshot != database_snapshot["snapshot_id"]:
        errors.append("database_snapshot_mismatch")
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(page.content):
            errors.append("prompt_injection_detected")
            break

    tables, columns = _schema_lookup(database_snapshot)
    result: list[KnowledgeChunk] = []
    for chunk_title, chunk_content in _chunks(page.title, page.content):
        dependencies: set[str] = set()
        explicit_columns = {
            "%s.%s" % match for match in _EXPLICIT_COLUMN.findall(chunk_content)
        }
        for column in explicit_columns:
            if column not in columns:
                errors.append("unknown_column:%s" % column)
            else:
                dependencies.add(column)
                dependencies.add(column.split(".", 1)[0])
        for table in _EXPLICIT_TABLE.findall(chunk_content):
            if table not in tables:
                errors.append("unknown_table:%s" % table)
            else:
                dependencies.add(table)
        content_sha = hashlib.sha256(chunk_content.encode("utf-8")).hexdigest()
        result.append(
            KnowledgeChunk(
                title=chunk_title,
                content=chunk_content,
                knowledge_type=chunk_type,
                dependencies=tuple(sorted(dependencies)),
                content_sha256=content_sha,
            )
        )
    return PageValidation(tuple(sorted(set(errors))), tuple(result))
