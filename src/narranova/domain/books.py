"""Book metadata and parsed EPUB documents."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class BookMetadata:
    title: str
    subtitle: str | None = None
    authors: tuple[str, ...] = ()
    language: str | None = None
    identifiers: tuple[str, ...] = ()
    publisher: str | None = None
    description: str | None = None
    series: str | None = None
    series_index: str | None = None


@dataclass(frozen=True)
class SourceElement:
    spine_index: int
    document: str
    element_id: str
    display_text: str
    kind: str = "paragraph"


@dataclass(frozen=True)
class SourceDocument:
    spine_index: int
    path: str
    title: str
    elements: tuple[SourceElement, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ParsedBook:
    metadata: BookMetadata
    documents: tuple[SourceDocument, ...]
    cover_path: str | None = None
    cover_media_type: str | None = None
    cover_data: bytes | None = None
