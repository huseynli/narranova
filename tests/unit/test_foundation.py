from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from narranova.artifacts import ArtifactLayout
from narranova.cli.main import main
from narranova.config import Settings
from narranova.persistence import Database
from narranova.providers import ProviderCapabilities, SynthesisRequest


class SettingsTests(unittest.TestCase):
    def test_explicit_data_directory_controls_database_location(self) -> None:
        settings = Settings.load("./relative-data")

        self.assertTrue(settings.data_dir.is_absolute())
        self.assertEqual(settings.database_path, settings.data_dir / "narranova.sqlite3")


class ArtifactLayoutTests(unittest.TestCase):
    def test_initializes_expected_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = ArtifactLayout.at(Path(temporary))
            layout.initialize()

            self.assertTrue(layout.books_root.is_dir())
            self.assertTrue(layout.temporary_root.is_dir())

    def test_rejects_path_traversal_in_ids(self) -> None:
        layout = ArtifactLayout.at(Path("/tmp/narranova-test"))

        with self.assertRaises(ValueError):
            layout.book_root("../outside")
        with self.assertRaises(ValueError):
            layout.chunk_master("book-1", "chapter/one")


class ProviderContractTests(unittest.TestCase):
    def test_capabilities_are_explicit_and_serializable(self) -> None:
        capabilities = ProviderCapabilities(streaming=True, supported_languages=("en",))

        self.assertTrue(capabilities.as_dict()["streaming"])
        self.assertEqual(capabilities.as_dict()["supported_languages"], ("en",))

    def test_synthesis_request_rejects_empty_text(self) -> None:
        with self.assertRaises(ValueError):
            SynthesisRequest(text="  ", destination=Path("chunk.wav"))


class DatabaseTests(unittest.TestCase):
    def test_initialization_is_idempotent_and_applies_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "narranova.sqlite3"
            database = Database(path)

            database.initialize()
            database.initialize()

            with sqlite3.connect(path) as connection:
                migrations = connection.execute(
                    "SELECT version, name FROM schema_migrations ORDER BY version"
                ).fetchall()
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }

            self.assertEqual(migrations, [(1, "initial"), (2, "generation_jobs")])
            self.assertTrue({"books", "jobs", "chunks", "artifacts"} <= tables)

    def test_cli_init_creates_database_and_artifact_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = main(["init", "--data-dir", temporary])

            self.assertEqual(result, 0)
            self.assertTrue((Path(temporary) / "narranova.sqlite3").is_file())
            self.assertTrue((Path(temporary) / "books").is_dir())


if __name__ == "__main__":
    unittest.main()
