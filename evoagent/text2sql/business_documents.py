"""Parse and validate trusted local business Markdown for Vanna."""
from __future__ import annotations
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
import yaml
from .models import KNOWLEDGE_TYPES


@dataclass(frozen=True)
class BusinessDocument:
    title: str
    content: str
    metadata: Mapping[str, Any]


def parse_markdown(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_text(encoding="utf-8-sig")
    if not raw.startswith("---\n"):
        raise ValueError("Business document must start with YAML frontmatter: %s" % path)
    end = raw.find("\n---\n", 4)
    if end < 0:
        raise ValueError("Business document frontmatter is not closed: %s" % path)
    metadata = yaml.safe_load(raw[4:end]) or {}
    if not isinstance(metadata, dict):
        raise ValueError("Business document frontmatter must be a mapping: %s" % path)
    return metadata, raw[end + 5 :].strip()


_INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions?", re.IGNORECASE),
    re.compile(r"system\s+prompt", re.IGNORECASE),
    re.compile(r"you\s+are\s+(?:chatgpt|an?\s+assistant)", re.IGNORECASE),
    re.compile(r"忽略.{0,12}(?:指令|提示词|规则)"),
    re.compile(r"(?:系统提示词|开发者指令|执行以下指令)"),
)
_EXPLICIT_COLUMN = re.compile(r"\b(t_[A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\b")
_EXPLICIT_TABLE = re.compile(r"\b(t_[A-Za-z0-9_]+)\b")
_PLANNING_BLOCK = re.compile(r"<!-- planning -->\s*([\s\S]*?)\s*<!-- /planning -->")
_KNOWLEDGE_STATUS = {
    "unreviewed": "未确认",
    "observed": "快照观测事实",
    "inferred": "依据 DDL、数据和领域资料推断",
    "project_convention": "本项目采用的默认口径",
    "candidate": "候选解释，业务口径尚未确认",
    "confirmed": "已确认业务口径",
}


@dataclass(frozen=True)
class DocumentChunk:
    title: str
    content: str
    knowledge_type: str
    dependencies: Sequence[str]
    content_sha256: str
    planning_content: str = ""
    knowledge_status: str = "unreviewed"


@dataclass(frozen=True)
class PageValidation:
    errors: Sequence[str]
    chunks: Sequence[DocumentChunk]


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
    page: BusinessDocument,
    database_snapshot: Mapping[str, Any],
) -> PageValidation:
    errors: list[str] = []
    metadata = page.metadata
    knowledge_type = str(metadata.get("knowledge_type") or "")
    if knowledge_type not in KNOWLEDGE_TYPES:
        errors.append("invalid_or_missing_knowledge_type")
    chunk_type = knowledge_type if knowledge_type in KNOWLEDGE_TYPES else "business_glossary"
    pinned_snapshot = str(metadata.get("database_snapshot_id") or "")
    if pinned_snapshot != database_snapshot["snapshot_id"]:
        errors.append("database_snapshot_mismatch")
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(page.content):
            errors.append("prompt_injection_detected")
            break

    tables, columns = _schema_lookup(database_snapshot)
    status = str(metadata.get("knowledge_status") or "unreviewed")
    if status not in _KNOWLEDGE_STATUS:
        errors.append("invalid_knowledge_status")
        status = "unreviewed"
    chunks = _chunks(page.title, page.content)
    section_status = metadata.get("section_status", {})
    if not isinstance(section_status, dict):
        errors.append("invalid_section_status")
        section_status = {}
    titles = {title for title, _ in chunks}
    for title, section_value in section_status.items():
        if title not in titles or str(section_value) not in _KNOWLEDGE_STATUS:
            errors.append("invalid_section_status:%s" % title)
    identifiers = tables | columns | {c.split(".", 1)[1] for c in columns}
    result: list[DocumentChunk] = []
    for chunk_title, chunk_content in chunks:
        chunk_status = str(section_status.get(chunk_title, status))
        if chunk_status not in _KNOWLEDGE_STATUS:
            chunk_status = "unreviewed"
        blocks = _PLANNING_BLOCK.findall(chunk_content)
        if (chunk_content.count("<!-- planning -->") != len(blocks)
                or chunk_content.count("<!-- /planning -->") != len(blocks)
                or len(blocks) > 1):
            errors.append("invalid_planning_block:%s" % chunk_title)
        planning_content = blocks[0].strip() if len(blocks) == 1 else ""
        if blocks and (not planning_content or knowledge_type != "business_glossary"):
            errors.append("invalid_planning_content:%s" % chunk_title)
        if planning_content:
            if any(re.search(r"(?<![a-z0-9_])%s(?![a-z0-9_])" % re.escape(name),
                             planning_content, re.I) for name in identifiers):
                errors.append("planning_schema_leak:%s" % chunk_title)
            if re.search(r"\b(?:select|insert|update|delete|create|drop|alter|with|pragma|values)\b",
                         planning_content, re.I):
                errors.append("planning_sql_leak:%s" % chunk_title)
        chunk_content = _PLANNING_BLOCK.sub(lambda m: m.group(1).strip(), chunk_content).strip()
        status_line = "内容状态：%s。" % _KNOWLEDGE_STATUS[chunk_status]
        chunk_content = status_line + "\n" + chunk_content
        if planning_content:
            planning_content = status_line + "\n" + planning_content
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
            DocumentChunk(
                title=chunk_title,
                content=chunk_content,
                knowledge_type=chunk_type,
                dependencies=tuple(sorted(dependencies)),
                content_sha256=content_sha,
                planning_content=planning_content,
                knowledge_status=chunk_status,
            )
        )
    return PageValidation(tuple(sorted(set(errors))), tuple(result))
