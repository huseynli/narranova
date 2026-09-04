from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
import wave
from importlib.resources import files
from pathlib import Path

from narranova.application.generation import GenerationJobs, VoiceProfiles
from narranova.artifacts import ArtifactLayout, ArtifactStore
from narranova.cli.main import main
from narranova.config import Settings
from narranova.persistence import Database
from narranova.persistence.books import BookRepository
from narranova.persistence.generation import GenerationRepository
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
            self.assertTrue(layout.benchmarks_root.is_dir())

    def test_rejects_path_traversal_in_ids(self) -> None:
        layout = ArtifactLayout.at(Path("/tmp/narranova-test"))

        with self.assertRaises(ValueError):
            layout.book_root("../outside")
        with self.assertRaises(ValueError):
            layout.chunk_master("book-1", "chapter/one")

    def test_stale_partial_cleanup_never_removes_promoted_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stale = root / "tmp" / ".generation-job-chunk.wav.part"
            final = root / "books" / "book" / "chunk.flac"
            stale.parent.mkdir(parents=True)
            final.parent.mkdir(parents=True)
            stale.write_bytes(b"partial")
            final.write_bytes(b"complete")
            os.utime(stale, (1, 1))

            removed = ArtifactStore(root).cleanup_abandoned_partials(
                older_than_seconds=1
            )

            self.assertEqual(removed, 1)
            self.assertFalse(stale.exists())
            self.assertTrue(final.exists())


class ProviderContractTests(unittest.TestCase):
    def test_capabilities_are_explicit_and_serializable(self) -> None:
        capabilities = ProviderCapabilities(streaming=True, supported_languages=("en",))

        self.assertTrue(capabilities.as_dict()["streaming"])
        self.assertEqual(capabilities.as_dict()["supported_languages"], ("en",))

    def test_synthesis_request_rejects_empty_text(self) -> None:
        with self.assertRaises(ValueError):
            SynthesisRequest(text="  ", destination=Path("chunk.wav"))


class DatabaseTests(unittest.TestCase):
    def test_migrates_book_bound_voices_and_jobs_to_global_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            layout = ArtifactLayout.at(data)
            layout.initialize()
            old_reference = layout.legacy_voice_reference("book", "voice")
            old_reference.parent.mkdir(parents=True)
            with wave.open(str(old_reference), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16_000)
                output.writeframes(b"\x01\x00" * 160)
            reference_hash = hashlib.sha256(old_reference.read_bytes()).hexdigest()
            profile = {
                "kind": "openmoss",
                "name": "Legacy narrator",
                "instruction": "A calm narrator.",
                "language": "English",
                "reference_artifact_path": old_reference.relative_to(data).as_posix(),
                "reference_sha256": reference_hash,
            }
            path = data / "narranova.sqlite3"
            migration_root = files("narranova.persistence.migrations")
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, "
                    "name TEXT NOT NULL, applied_at TEXT DEFAULT CURRENT_TIMESTAMP)"
                )
                connection.executescript(
                    migration_root.joinpath("001_initial.sql").read_text(encoding="utf-8")
                )
                connection.executescript(
                    migration_root.joinpath("002_generation_jobs.sql").read_text(encoding="utf-8")
                )
                connection.executemany(
                    "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                    [(1, "initial"), (2, "generation_jobs")],
                )
                connection.execute(
                    "INSERT INTO provider_instances(id, kind, name, endpoint_url) "
                    "VALUES ('provider', 'openmoss', 'MOSS', 'http://moss/tts')"
                )
                connection.execute(
                    "INSERT INTO books(id, source_sha256, source_artifact_path) "
                    "VALUES ('book', 'source-hash', 'books/book/source/original.epub')"
                )
                connection.execute(
                    "INSERT INTO voice_profiles(id, book_id, provider_instance_id, "
                    "profile_json, profile_sha256) VALUES (?, ?, ?, ?, ?)",
                    ("voice", "book", "provider", json.dumps(profile), "old-profile-hash"),
                )
                connection.execute(
                    "INSERT INTO jobs(id, book_id, voice_profile_id, status) "
                    "VALUES ('job', 'book', 'voice', 'ready')"
                )

            database = Database(path)
            database.initialize()
            repository = GenerationRepository(database)
            store = ArtifactStore(data)
            VoiceProfiles(repository, layout, store)
            GenerationJobs(BookRepository(database), repository, layout, store)

            migrated = repository.get_voice_and_provider("voice")
            migrated_reference = data / migrated["profile"]["reference_artifact_path"]
            migrated_job = repository.get_job("job")
            job_reference = data / migrated_job["profile"]["reference_artifact_path"]
            self.assertEqual(migrated_job["narrator_profile_id"], "voice")
            self.assertEqual(migrated_job["provider_instance_id"], "provider")
            self.assertEqual(migrated["profile"]["name"], "Legacy narrator")
            self.assertTrue(migrated_reference.is_file())
            self.assertTrue(migrated_reference.is_relative_to(layout.voices_root))
            self.assertTrue(job_reference.is_file())
            self.assertTrue(job_reference.is_relative_to(layout.job_root("book", "job")))
            self.assertFalse(old_reference.exists())

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

            self.assertEqual(
                migrations,
                [
                    (1, "initial"),
                    (2, "generation_jobs"),
                    (3, "global_narrator_profiles"),
                    (4, "self_contained_job_voices"),
                    (5, "output_artifacts"),
                    (6, "job_storage"),
                    (8, "connection_performance"),
                    (9, "chapter_pause"),
                    (10, "work_leases"),
                    (11, "chunk_attempts"),
                    (12, "narration_enhancement"),
                ],
            )
            self.assertTrue(
                {
                    "books",
                    "jobs",
                    "book_narration_enhancement",
                    "chunks",
                    "artifacts",
                    "narrator_profiles",
                    "connection_benchmark_runs",
                }
                <= tables
            )

    def test_cli_init_creates_database_and_artifact_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = main(["init", "--data-dir", temporary])

            self.assertEqual(result, 0)
            self.assertTrue((Path(temporary) / "narranova.sqlite3").is_file())
            self.assertTrue((Path(temporary) / "books").is_dir())
            self.assertTrue((Path(temporary) / "voices").is_dir())


if __name__ == "__main__":
    unittest.main()
