"""A local Markdown implementation of the platform-neutral WikiConnector."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import yaml

from .wiki_connector import WikiACL, WikiChange, WikiPage, WikiSpace


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _decode_cursor(cursor: Optional[str]) -> dict[str, Mapping[str, str]]:
    if not cursor:
        return {}
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid Markdown Wiki cursor") from exc
    if not isinstance(value, dict):
        raise ValueError("invalid Markdown Wiki cursor payload")
    return value


def _encode_cursor(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def parse_markdown(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_text(encoding="utf-8-sig")
    if not raw.startswith("---\n"):
        raise ValueError("Wiki page must start with YAML frontmatter: %s" % path)
    end = raw.find("\n---\n", 4)
    if end < 0:
        raise ValueError("Wiki page frontmatter is not closed: %s" % path)
    metadata = yaml.safe_load(raw[4:end]) or {}
    if not isinstance(metadata, dict):
        raise ValueError("Wiki page frontmatter must be a mapping: %s" % path)
    return metadata, raw[end + 5 :].strip()


class MarkdownWikiConnector:
    """Treat a directory of reviewed Markdown files as a Wiki source."""

    def __init__(self, root: Path, space_id: str = "local-text2sql-wiki") -> None:
        self.root = root.resolve()
        self.space_id = space_id

    def _paths(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(
            path
            for path in self.root.rglob("*.md")
            if path.is_file() and not any(part.startswith(".") for part in path.relative_to(self.root).parts)
        )

    def _inventory(self) -> dict[str, dict[str, str]]:
        inventory: dict[str, dict[str, str]] = {}
        page_ids: set[str] = set()
        for path in self._paths():
            metadata, _ = parse_markdown(path)
            relative = path.relative_to(self.root).as_posix()
            page_id = str(metadata.get("page_id") or "md-" + _sha256(relative)[:20])
            if page_id in page_ids:
                raise ValueError("duplicate Wiki page_id: %s" % page_id)
            page_ids.add(page_id)
            content_sha = _sha256(path.read_text(encoding="utf-8-sig"))
            inventory[relative] = {"page_id": page_id, "sha256": content_sha}
        return inventory

    def list_spaces(self) -> Sequence[WikiSpace]:
        return [WikiSpace(self.space_id, self.root.name)]

    def list_changes(self, cursor: Optional[str] = None) -> Sequence[WikiChange]:
        previous = _decode_cursor(cursor)
        current = self._inventory()
        next_cursor = _encode_cursor(current)
        changes: list[WikiChange] = []
        for relative, item in current.items():
            old = previous.get(relative)
            if old != item:
                changes.append(
                    WikiChange(item["page_id"], item["sha256"], "upsert", next_cursor)
                )
        for relative, item in previous.items():
            if relative not in current:
                changes.append(
                    WikiChange(item["page_id"], item["sha256"], "revoke", next_cursor)
                )
        return sorted(changes, key=lambda item: (item.page_id, item.change_type))

    def _find(self, page_id: str) -> tuple[Path, dict[str, Any], str]:
        for path in self._paths():
            metadata, content = parse_markdown(path)
            relative = path.relative_to(self.root).as_posix()
            current_id = str(metadata.get("page_id") or "md-" + _sha256(relative)[:20])
            if current_id == page_id:
                return path, metadata, content
        raise KeyError(page_id)

    def fetch_page(self, page_id: str, version: str) -> WikiPage:
        path, metadata, content = self._find(page_id)
        actual_version = _sha256(path.read_text(encoding="utf-8-sig"))
        if actual_version != version:
            raise ValueError("Wiki page changed after list_changes: %s" % page_id)
        updated_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        return WikiPage(
            page_id=page_id,
            page_version=actual_version,
            title=str(metadata.get("title") or path.stem),
            content=content,
            owner_id=str(metadata.get("owner_id") or ""),
            updated_at=updated_at,
            metadata={**metadata, "relative_path": path.relative_to(self.root).as_posix()},
        )

    def fetch_acl(self, page_id: str) -> WikiACL:
        _, metadata, _ = self._find(page_id)
        principals = metadata.get("allowed_principals") or []
        if isinstance(principals, str):
            principals = [principals]
        return WikiACL(page_id, tuple(str(item) for item in principals))

    def resolve_link(self, page_id: str) -> str:
        path, _, _ = self._find(page_id)
        return str(path)
