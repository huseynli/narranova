from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from narranova.application.ingest import ImportBook
from narranova.application.revise_plan import ReviseNarrationPlan
from narranova.artifacts import ArtifactLayout, ArtifactStore
from narranova.domain.narration import NarrationPlan
from narranova.epub import EpubParser
from narranova.persistence import Database
from narranova.persistence.books import BookRepository
from tests.unit.test_epub_ingest import make_epub


class PlanRevisionTests(unittest.TestCase):
    def test_chapter_choices_create_an_immutable_plan_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            make_epub(source)
            data = root / "data"
            layout = ArtifactLayout.at(data)
            layout.initialize()
            store = ArtifactStore(data)
            database = Database(data / "narranova.sqlite3")
            database.initialize()
            books = BookRepository(database)
            imported = ImportBook(EpubParser(), books, layout, store).execute(source)
            revisions = ReviseNarrationPlan(books, layout, store)

            result = revisions.execute(imported.book_id, {2})

            record = books.get_plan_record(imported.book_id)
            plan = NarrationPlan.from_json(
                (data / record["artifact_path"]).read_text(encoding="utf-8")
            )
            self.assertTrue(layout.plan(imported.book_id, 1).is_file())
            self.assertEqual(result.revision, 2)
            self.assertEqual(record["revision"], 2)
            self.assertTrue(all(not unit.enabled for unit in plan.units if unit.spine_index == 1))
            self.assertTrue(all(unit.enabled for unit in plan.units if unit.spine_index == 2))
            self.assertEqual(
                [decision["enabled"] for decision in plan.metadata["narration_decisions"]],
                [False, True],
            )

            unchanged = revisions.execute(imported.book_id, {2})
            self.assertFalse(unchanged.changed)
            self.assertEqual(unchanged.revision, 2)
            self.assertFalse(layout.plan(imported.book_id, 3).exists())


if __name__ == "__main__":
    unittest.main()
