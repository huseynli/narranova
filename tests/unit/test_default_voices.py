from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from narranova.application.default_voices import default_voice_pair, default_voice_pairs
from narranova.application.generation import GenerationJobs, VoiceProfiles
from narranova.application.ingest import ImportBook
from narranova.artifacts import ArtifactLayout, ArtifactStore
from narranova.epub import EpubParser
from narranova.persistence import Database
from narranova.persistence.books import BookRepository
from narranova.persistence.generation import GenerationRepository
from tests.unit.test_epub_ingest import make_epub
from tests.unit.test_generation_jobs import FakeAudioMasters, FakeProvider


class DefaultVoiceTests(unittest.TestCase):
    def test_catalog_contains_two_valid_exact_pairs(self) -> None:
        pairs = default_voice_pairs()

        self.assertEqual(len(pairs), 2)
        self.assertEqual([pair.id for pair in pairs], ["04", "09"])
        self.assertTrue(all(pair.audio_path.is_file() for pair in pairs))
        self.assertTrue(all(len(pair.audio_sha256) == 64 for pair in pairs))
        self.assertIn("mature audiobook narrator", default_voice_pair("builtin:04").instruction)

    def test_builtin_pair_creates_a_self_contained_job_without_saved_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            source = root / "book.epub"
            make_epub(source)
            layout = ArtifactLayout.at(data)
            layout.initialize()
            store = ArtifactStore(data)
            database = Database(data / "narranova.sqlite3")
            database.initialize()
            books = BookRepository(database)
            generation = GenerationRepository(database)
            imported = ImportBook(EpubParser(), books, layout, store).execute(source)
            profiles = VoiceProfiles(generation, layout, store)
            provider_id = profiles.add_openmoss_provider(
                "Test MOSS", "http://moss.test:8000/tts"
            )
            fake = FakeProvider()
            jobs = GenerationJobs(
                books,
                generation,
                layout,
                store,
                provider_factory=lambda job: fake,
                masters=FakeAudioMasters(),
            )

            job_id = jobs.create(imported.book_id, "builtin:04", provider_id)
            job = generation.get_job(job_id)
            reference = data / job["profile"]["reference_artifact_path"]

            self.assertIsNone(job["narrator_profile_id"])
            self.assertEqual(job["profile"]["builtin_voice_id"], "04")
            self.assertEqual(job["profile"]["instruction"], default_voice_pair("builtin:04").instruction)
            self.assertTrue(reference.is_file())
            self.assertTrue(reference.is_relative_to(layout.job_root(imported.book_id, job_id)))

            jobs.run(job_id)
            self.assertEqual(generation.get_job(job_id)["status"], "completed")
            self.assertTrue(fake.requests)


if __name__ == "__main__":
    unittest.main()
