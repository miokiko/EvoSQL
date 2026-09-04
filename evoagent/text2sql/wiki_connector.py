"""Platform-neutral Wiki boundary for Text2SQL knowledge ingestion.

Concrete adapters (for example Feishu Wiki or Confluence) belong behind this
interface. SQL request handling must query a local, versioned KnowledgeStore;
it must not make synchronous calls to a Wiki platform.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, Sequence


@dataclass(frozen=True)
class WikiSpace:
    space_id: str
    name: str


@dataclass(frozen=True)
class WikiChange:
    page_id: str
    page_version: str
    change_type: str
    cursor: str


@dataclass(frozen=True)
class WikiPage:
    page_id: str
    page_version: str
    title: str
    content: str
    owner_id: str
    updated_at: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WikiACL:
    page_id: str
    principals: Sequence[str]
    inherited_from: Optional[str] = None


class WikiConnector(Protocol):
    def list_spaces(self) -> Sequence[WikiSpace]: ...

    def list_changes(self, cursor: Optional[str] = None) -> Sequence[WikiChange]: ...

    def fetch_page(self, page_id: str, version: str) -> WikiPage: ...

    def fetch_acl(self, page_id: str) -> WikiACL: ...

    def resolve_link(self, page_id: str) -> str: ...
