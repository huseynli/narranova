"""Import an EPUB and create its first immutable narration plan revision."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from narranova.artifacts import ArtifactLayout, ArtifactStore
from narranova.domain.narration import NarrationPlan, plan_book
from narranova.epub import EpubParser
from narranova.persistence.books import BookRepository


@dataclass(frozen=True)
class ImportResult:
    book_id: str
    title: str
    chapter_count: int
    unit_count: int
    plan: NarrationPlan


class ImportBook:
    def __init__(
        self,
        parser: EpubParser,
        repository: BookRepository,
        layout: ArtifactLayout,
        store: ArtifactStore,
    ) -> None:
        self.parser = parser
        self.repository = repository
        self.layout = layout
        self.store = store

    def execute(self, source: Path) -> ImportResult:
        source = source.expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"EPUB not found: {source}")
        parsed = self.parser.parse(source)
        plan = plan_book(parsed)
        book_id = uuid.uuid4().hex
        plan_id = uuid.uuid4().hex
        source_path = self.layout.source_epub(book_id)
        plan_path = self.layout.plan(book_id, 1)
        source_hash = self.store.copy(source, source_path)
        plan_hash = self.store.write_text(plan_path, plan.to_json())
        try:
            self.repository.add_book_with_plan(
                book_id=book_id,
                title=parsed.metadata.title,
                author=", ".join(parsed.metadata.authors) or None,
                language=parsed.metadata.language,
                source_sha256=source_hash,
                source_path=source_path.relative_to(self.layout.root).as_posix(),
                plan_id=plan_id,
                plan_sha256=plan_hash,
                plan_path=plan_path.relative_to(self.layout.root).as_posix(),
            )
        except Exception:
            plan_path.unlink(missing_ok=True)
            source_path.unlink(missing_ok=True)
            book_root = self.layout.book_root(book_id)
            for directory in (plan_path.parent, source_path.parent, book_root):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            raise
        return ImportResult(book_id, parsed.metadata.title, len(plan.chapters), len(plan.units), plan)
