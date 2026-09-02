"""Create immutable narration-plan revisions from review choices."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from narranova.artifacts import ArtifactLayout, ArtifactStore
from narranova.domain.narration import NarrationPlan
from narranova.persistence.books import BookRepository


@dataclass(frozen=True)
class RevisionResult:
    revision: int
    enabled_chapters: int
    enabled_units: int
    changed: bool


class ReviseNarrationPlan:
    def __init__(
        self,
        books: BookRepository,
        layout: ArtifactLayout,
        store: ArtifactStore,
    ) -> None:
        self.books = books
        self.layout = layout
        self.store = store

    def execute(self, book_id: str, enabled_spine_indices: set[int]) -> RevisionResult:
        record = self.books.get_plan_record(book_id)
        source_path = self._artifact(record["artifact_path"])
        if self.store.sha256(source_path) != record["plan_sha256"]:
            raise RuntimeError("Narration plan failed hash validation")
        current = NarrationPlan.from_json(source_path.read_text(encoding="utf-8"))
        known_indices = {chapter.spine_index for chapter in current.chapters}
        unknown = enabled_spine_indices - known_indices
        if unknown:
            raise ValueError(f"Unknown narration section(s): {', '.join(map(str, sorted(unknown)))}")
        desired = tuple(
            replace(unit, enabled=unit.spine_index in enabled_spine_indices)
            for unit in current.units
        )
        enabled_chapters = len(enabled_spine_indices)
        enabled_units = sum(unit.enabled for unit in desired)
        if all(before.enabled == after.enabled for before, after in zip(current.units, desired)):
            return RevisionResult(current.revision, enabled_chapters, enabled_units, False)
        revision = int(record["revision"]) + 1
        metadata = dict(current.metadata)
        metadata["narration_decisions"] = [
            {
                "scope": "chapter",
                "spine_index": chapter.spine_index,
                "document": chapter.document,
                "enabled": chapter.spine_index in enabled_spine_indices,
            }
            for chapter in current.chapters
        ]
        revised = replace(
            current,
            revision=revision,
            metadata=metadata,
            units=desired,
        )
        revised.validate()
        destination = self.layout.plan(book_id, revision)
        plan_hash = self.store.write_text(destination, revised.to_json())
        try:
            self.books.add_plan_revision(
                plan_id=uuid.uuid4().hex,
                book_id=book_id,
                revision=revision,
                plan_sha256=plan_hash,
                artifact_path=destination.relative_to(self.layout.root).as_posix(),
            )
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return RevisionResult(revision, enabled_chapters, enabled_units, True)

    def _artifact(self, relative_path: str) -> Path:
        path = (self.layout.root / relative_path).resolve()
        if not path.is_relative_to(self.layout.root):
            raise RuntimeError("Stored narration plan path escapes the data directory")
        return path
