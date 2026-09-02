from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from narranova.application.generation import VoiceProfiles
from narranova.application.ingest import ImportBook
from narranova.application.voice_studio import DEFAULT_SAMPLE_TEXT, VoiceStudio
from narranova.artifacts import ArtifactLayout, ArtifactStore
from narranova.epub import EpubParser
from narranova.persistence import Database
from narranova.persistence.books import BookRepository
from narranova.persistence.generation import GenerationRepository
from tests.unit.test_epub_ingest import make_epub
from tests.unit.test_generation_jobs import FakeProvider, make_wave


class VoiceStudioTests(unittest.TestCase):
    def test_auditions_promote_one_named_pair_and_delete_the_draft(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            source = root / "book.epub"
            reference = root / "reference.wav"
            make_epub(source)
            make_wave(reference)
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
            studio = VoiceStudio(
                books,
                generation,
                profiles,
                layout,
                store,
                provider_factory=lambda provider: fake,
            )
            draft_id = studio.start(imported.book_id)

            first_take = studio.generate_take(
                draft_id,
                provider_id=provider_id,
                reference_choice="",
                instruction="A restrained literary narrator.",
                sample_text=DEFAULT_SAMPLE_TEXT,
                language="English",
                uploaded_reference=reference,
            )
            second_take = studio.generate_take(
                draft_id,
                provider_id=provider_id,
                reference_choice=f"take:{first_take}",
                instruction="A warmer literary narrator with measured pacing.",
                sample_text=DEFAULT_SAMPLE_TEXT,
                language="English",
            )
            draft = studio.get(draft_id)
            selected_hash = next(
                take["audio_sha256"] for take in draft["takes"] if take["id"] == second_take
            )

            profile_id, book_id = studio.save_profile(
                draft_id,
                name="Warm literary",
                provider_id=provider_id,
                reference_choice=f"take:{second_take}",
                instruction="A warmer literary narrator with measured pacing.",
                language="English",
            )

            saved = generation.get_voice_and_provider(profile_id)
            saved_path = data / saved["profile"]["reference_artifact_path"]
            self.assertEqual(book_id, imported.book_id)
            self.assertEqual(saved["profile"]["name"], "Warm literary")
            self.assertEqual(store.sha256(saved_path), selected_hash)
            self.assertEqual(len(fake.requests), 2)
            self.assertFalse(layout.voice_studio_draft(draft_id).exists())

    def test_generation_failure_keeps_uploaded_reference_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            source = root / "book.epub"
            reference = root / "reference.wav"
            make_epub(source)
            make_wave(reference)
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
                "Unavailable MOSS", "http://moss.test:8000/tts"
            )

            class FailingProvider:
                def synthesize(self, request):
                    raise RuntimeError("MOSS is offline")

            studio = VoiceStudio(
                books,
                generation,
                profiles,
                layout,
                store,
                provider_factory=lambda provider: FailingProvider(),
            )
            draft_id = studio.start(imported.book_id)

            with self.assertRaisesRegex(RuntimeError, "offline"):
                studio.generate_take(
                    draft_id,
                    provider_id=provider_id,
                    reference_choice="",
                    instruction="A careful narrator.",
                    sample_text=DEFAULT_SAMPLE_TEXT,
                    language="English",
                    uploaded_reference=reference,
                )

            draft = studio.get(draft_id)
            self.assertTrue(draft["uploaded_reference_path"])
            self.assertTrue(layout.voice_studio_upload(draft_id).is_file())
            self.assertEqual(draft["takes"], [])


if __name__ == "__main__":
    unittest.main()
