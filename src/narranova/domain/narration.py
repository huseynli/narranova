"""Reviewable narration plan types and integrity checks."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from narranova.domain.books import ParsedBook


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class NarrationUnit:
    id: str
    spine_index: int
    document: str
    element_id: str
    display_text: str
    spoken_text: str
    enabled: bool
    display_text_sha256: str
    spoken_text_sha256: str


@dataclass(frozen=True)
class NarrationChapter:
    spine_index: int
    document: str
    title: str
    unit_ids: tuple[str, ...]


@dataclass(frozen=True)
class NarrationPlan:
    schema_version: int
    revision: int
    metadata: dict[str, Any]
    chapters: tuple[NarrationChapter, ...]
    units: tuple[NarrationUnit, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "metadata": self.metadata,
            "chapters": [asdict(chapter) for chapter in self.chapters],
            "units": [asdict(unit) for unit in self.units],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=2) + "\n"

    @property
    def sha256(self) -> str:
        return text_sha256(self.to_json())

    def validate(self) -> None:
        ids = [unit.id for unit in self.units]
        if len(ids) != len(set(ids)):
            raise ValueError("Narration plan contains duplicate unit IDs")
        known_ids = set(ids)
        referenced = [unit_id for chapter in self.chapters for unit_id in chapter.unit_ids]
        if len(referenced) != len(set(referenced)):
            raise ValueError("A narration unit is referenced by more than one chapter")
        if set(referenced) != known_ids:
            raise ValueError("Every narration unit must be referenced by exactly one chapter")
        for unit in self.units:
            if not unit.display_text.strip() or not unit.spoken_text.strip():
                raise ValueError(f"Narration unit {unit.id} contains empty text")
            if text_sha256(unit.display_text) != unit.display_text_sha256:
                raise ValueError(f"Narration unit {unit.id} display hash is invalid")
            if text_sha256(unit.spoken_text) != unit.spoken_text_sha256:
                raise ValueError(f"Narration unit {unit.id} spoken hash is invalid")


def plan_book(book: ParsedBook, revision: int = 1) -> NarrationPlan:
    units: list[NarrationUnit] = []
    chapters: list[NarrationChapter] = []
    for document in book.documents:
        chapter_unit_ids: list[str] = []
        for position, element in enumerate(document.elements, 1):
            unit_id = f"s{document.spine_index:04d}-u{position:05d}"
            spoken_text = element.display_text
            unit = NarrationUnit(
                id=unit_id,
                spine_index=document.spine_index,
                document=document.path,
                element_id=element.element_id,
                display_text=element.display_text,
                spoken_text=spoken_text,
                enabled=True,
                display_text_sha256=text_sha256(element.display_text),
                spoken_text_sha256=text_sha256(spoken_text),
            )
            units.append(unit)
            chapter_unit_ids.append(unit_id)
        chapters.append(
            NarrationChapter(
                spine_index=document.spine_index,
                document=document.path,
                title=document.title,
                unit_ids=tuple(chapter_unit_ids),
            )
        )
    metadata = asdict(book.metadata)
    plan = NarrationPlan(
        schema_version=1,
        revision=revision,
        metadata=metadata,
        chapters=tuple(chapters),
        units=tuple(units),
    )
    plan.validate()
    return plan
