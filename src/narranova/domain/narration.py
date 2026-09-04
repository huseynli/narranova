"""Reviewable narration plan types and integrity checks."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from narranova.domain.books import ParsedBook
from narranova.domain.enhancement import is_scene_break


_UNIT_KINDS = {"paragraph", "chapter_heading", "section_heading", "scene_break"}


def _legacy_unit_kind(unit: dict[str, Any], first_units: set[str]) -> str:
    """Conservatively recover structure from plans created before kind metadata."""
    if str(unit["id"]) in first_units:
        return "chapter_heading"
    text = str(unit.get("display_text", "")).strip()
    if is_scene_break(text):
        return "scene_break"
    if (
        text
        and len(text) <= 120
        and len(text.split()) <= 12
        and not re.search(r"[.!?…][\"']?$", text)
    ):
        return "section_heading"
    return "paragraph"


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
    kind: str = "paragraph"


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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NarrationPlan":
        chapters = tuple(
            NarrationChapter(
                spine_index=int(chapter["spine_index"]),
                document=str(chapter["document"]),
                title=str(chapter["title"]),
                unit_ids=tuple(chapter["unit_ids"]),
            )
            for chapter in data["chapters"]
        )
        first_units = {chapter.unit_ids[0] for chapter in chapters if chapter.unit_ids}
        units = []
        for raw_unit in data["units"]:
            values = dict(raw_unit)
            if "kind" not in values:
                values["kind"] = _legacy_unit_kind(values, first_units)
            units.append(NarrationUnit(**values))
        plan = cls(
            schema_version=int(data["schema_version"]),
            revision=int(data["revision"]),
            metadata=dict(data["metadata"]),
            chapters=chapters,
            units=tuple(units),
        )
        if plan.schema_version != 1:
            raise ValueError(f"Unsupported narration plan schema: {plan.schema_version}")
        plan.validate()
        return plan

    @classmethod
    def from_json(cls, content: str) -> "NarrationPlan":
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("Narration plan must be a JSON object")
        return cls.from_dict(data)

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
            if unit.kind not in _UNIT_KINDS:
                raise ValueError(f"Narration unit {unit.id} has an invalid kind")
            if unit.kind != "scene_break" and (
                not unit.display_text.strip() or not unit.spoken_text.strip()
            ):
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
                kind=element.kind,
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
