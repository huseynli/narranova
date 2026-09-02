from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from narranova.application.deletion import DeleteArtifacts
from narranova.application.generation import GenerationJobs, VoiceProfiles
from narranova.application.ingest import ImportBook
from narranova.artifacts import ArtifactLayout, ArtifactStore
from narranova.epub import EpubParser
from narranova.persistence import Database
from narranova.persistence.books import BookRepository
from narranova.persistence.generation import GenerationRepository
from tests.unit.test_epub_ingest import make_epub
from tests.unit.test_generation_jobs import FakeProvider, make_wave


def make_workspace(root: Path) -> dict[str, object]:
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
    provider_id = profiles.add_openmoss_provider("Test MOSS", "http://moss.test:8000/tts")
    profile_id = profiles.create_openmoss_profile(
        provider_id=provider_id,
        reference_audio=reference,
        instruction="A careful narrator.",
    )
    jobs = GenerationJobs(
        books,
        generation,
        layout,
        store,
        provider_factory=lambda job: FakeProvider(),
    )
    job_id = jobs.create(imported.book_id, profile_id)
    return {
        "books": books,
        "generation": generation,
        "layout": layout,
        "deletion": DeleteArtifacts(books, generation, layout),
        "jobs": jobs,
        "book_id": imported.book_id,
        "job_id": job_id,
        "profile_id": profile_id,
    }


class DeleteArtifactsTests(unittest.TestCase):
    def test_generated_chunk_audio_is_deleted_and_returns_to_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = make_workspace(Path(temporary))
            jobs = workspace["jobs"]
            generation = workspace["generation"]
            layout = workspace["layout"]
            deletion = workspace["deletion"]
            job_id = str(workspace["job_id"])
            jobs.run(job_id)  # type: ignore[union-attr]
            chunk = generation.list_chunks(job_id)[0]  # type: ignore[union-attr]
            audio_path = layout.root / chunk.audio_artifact_path  # type: ignore[union-attr,operator]
            self.assertTrue(audio_path.is_file())

            deletion.generated_chunk(job_id, chunk.database_id)  # type: ignore[union-attr]

            updated = generation.get_chunk(job_id, chunk.database_id)  # type: ignore[union-attr]
            self.assertFalse(audio_path.exists())
            self.assertEqual(updated.status, "pending")
            self.assertIsNone(updated.audio_artifact_path)
            self.assertEqual(generation.get_job(job_id)["status"], "ready")  # type: ignore[union-attr]

    def test_job_deletion_removes_job_artifacts_but_keeps_book(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = make_workspace(Path(temporary))
            generation = workspace["generation"]
            layout = workspace["layout"]
            deletion = workspace["deletion"]
            book_id = str(workspace["book_id"])
            job_id = str(workspace["job_id"])
            job_root = layout.job_root(book_id, job_id)  # type: ignore[union-attr]
            self.assertTrue(job_root.is_dir())

            deletion.job(job_id)  # type: ignore[union-attr]

            self.assertFalse(job_root.exists())
            self.assertEqual(workspace["books"].get_book(book_id).id, book_id)  # type: ignore[union-attr]
            with self.assertRaises(KeyError):
                generation.get_job(job_id)  # type: ignore[union-attr]

    def test_book_deletion_cascades_records_and_removes_all_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = make_workspace(Path(temporary))
            books = workspace["books"]
            generation = workspace["generation"]
            layout = workspace["layout"]
            deletion = workspace["deletion"]
            book_id = str(workspace["book_id"])
            job_id = str(workspace["job_id"])
            profile_id = str(workspace["profile_id"])
            profile_root = layout.voice_profile_root(profile_id)  # type: ignore[union-attr]
            book_root = layout.book_root(book_id)  # type: ignore[union-attr]

            deletion.book(book_id)  # type: ignore[union-attr]

            self.assertFalse(book_root.exists())
            with self.assertRaises(KeyError):
                books.get_book(book_id)  # type: ignore[union-attr]
            with self.assertRaises(KeyError):
                generation.get_job(job_id)  # type: ignore[union-attr]
            self.assertTrue(profile_root.is_dir())
            self.assertEqual(
                generation.get_voice_and_provider(profile_id)["profile"]["name"],  # type: ignore[union-attr]
                "Narrator profile",
            )

    def test_voice_profile_deletion_removes_its_global_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = make_workspace(Path(temporary))
            generation = workspace["generation"]
            layout = workspace["layout"]
            deletion = workspace["deletion"]
            jobs = workspace["jobs"]
            job_id = str(workspace["job_id"])
            profile_id = str(workspace["profile_id"])
            profile_root = layout.voice_profile_root(profile_id)  # type: ignore[union-attr]
            job_reference = layout.job_voice_reference(  # type: ignore[union-attr]
                str(workspace["book_id"]), job_id
            )

            with self.assertRaisesRegex(ValueError, "in use by 1 unfinished"):
                deletion.voice_profile(profile_id)  # type: ignore[union-attr]
            jobs.run(job_id)  # type: ignore[union-attr]
            deletion.voice_profile(profile_id)  # type: ignore[union-attr]

            self.assertFalse(profile_root.exists())
            self.assertTrue(job_reference.is_file())
            with self.assertRaises(KeyError):
                generation.get_voice_and_provider(profile_id)  # type: ignore[union-attr]
            self.assertIsNone(generation.get_job(job_id)["narrator_profile_id"])  # type: ignore[union-attr]
            self.assertEqual(generation.get_job(job_id)["status"], "completed")  # type: ignore[union-attr]

    def test_active_jobs_cannot_be_deleted_with_their_book_or_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = make_workspace(Path(temporary))
            generation = workspace["generation"]
            deletion = workspace["deletion"]
            book_id = str(workspace["book_id"])
            job_id = str(workspace["job_id"])
            chunk = generation.list_chunks(job_id)[0]  # type: ignore[union-attr]
            generation.begin_chunk(job_id, chunk.database_id)  # type: ignore[union-attr]

            with self.assertRaisesRegex(ValueError, "Pause"):
                deletion.generated_chunk(job_id, chunk.database_id)  # type: ignore[union-attr]
            with self.assertRaisesRegex(ValueError, "Pause"):
                deletion.job(job_id)  # type: ignore[union-attr]
            with self.assertRaisesRegex(ValueError, "Pause"):
                deletion.book(book_id)  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
