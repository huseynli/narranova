from __future__ import annotations

import hashlib
import tempfile
import unittest
import wave
from pathlib import Path

from narranova.application.generation import (
    GenerationJobs,
    VoiceProfiles,
    deterministic_chunk_seed,
)
from narranova.application.ingest import ImportBook
from narranova.application.revise_plan import ReviseNarrationPlan
from narranova.artifacts import ArtifactLayout, ArtifactStore
from narranova.audio import AudioMasterInfo, validate_wave
from narranova.epub import EpubParser
from narranova.persistence import Database
from narranova.persistence.benchmarks import BenchmarkRepository
from narranova.persistence.books import BookRepository
from narranova.persistence.generation import GenerationRepository
from narranova.providers.base import SynthesisRequest, SynthesisResult
from tests.unit.test_epub_ingest import make_epub


def make_wave(path: Path, frames: int = 240) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24_000)
        output.writeframes(b"\x01\x00" * frames)


class FakeProvider:
    def __init__(self) -> None:
        self.requests: list[SynthesisRequest] = []
        self.failure_message: str | None = None

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        self.requests.append(request)
        if self.failure_message:
            raise RuntimeError(self.failure_message)
        make_wave(request.destination)
        digest = hashlib.sha256(request.destination.read_bytes()).hexdigest()
        return SynthesisResult(request.destination, digest, 0.01)


class FakeAudioMasters:
    def normalize(self, source: Path, destination: Path) -> AudioMasterInfo:
        source_info = validate_wave(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        return AudioMasterInfo(
            destination,
            "flac",
            1,
            48_000,
            source_info.duration_seconds,
            destination.stat().st_size,
        )

    def validate(self, path: Path) -> AudioMasterInfo:
        info = validate_wave(path)
        return AudioMasterInfo(
            path,
            "flac",
            1,
            48_000,
            info.duration_seconds,
            path.stat().st_size,
        )


class GenerationJobTests(unittest.TestCase):
    def test_chunk_seed_is_stable_distinct_and_openmoss_safe(self) -> None:
        first = deterministic_chunk_seed("book-a", 1, 2)

        self.assertEqual(first, deterministic_chunk_seed("book-a", 1, 2))
        self.assertNotEqual(first, deterministic_chunk_seed("book-a", 1, 3))
        self.assertNotEqual(first, deterministic_chunk_seed("book-b", 1, 2))
        self.assertGreater(first, 0)
        self.assertLessEqual(first, 2_147_483_647)

    def test_job_generates_resumes_and_repairs_corrupt_completed_audio(self) -> None:
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
            profile_id = profiles.create_openmoss_profile(
                provider_id=provider_id,
                reference_audio=reference,
                instruction="A careful narrator.",
                name="Careful narrator",
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

            benchmarks = BenchmarkRepository(database)
            benchmarks.apply_frames(
                provider_id=provider_id,
                selected_frames=128,
                recommended_frames=128,
                benchmark_id="first-benchmark",
            )

            job_id = jobs.create(imported.book_id, profile_id)
            benchmarks.apply_frames(
                provider_id=provider_id,
                selected_frames=256,
                recommended_frames=256,
                benchmark_id="later-benchmark",
            )
            self.assertEqual(
                generation.get_job(job_id)["provider_configuration"][
                    "stream_chunk_frames"
                ],
                128,
            )
            profiles.update_openmoss_profile(
                profile_id,
                provider_id=provider_id,
                instruction="A newly edited narrator.",
                name="Edited narrator",
            )
            self.assertEqual(
                generation.get_job(job_id)["profile"]["instruction"],
                "A careful narrator.",
            )
            other_provider_id = profiles.add_openmoss_provider(
                "Other MOSS", "http://other-moss.test:8000/tts"
            )
            with self.assertRaisesRegex(ValueError, "selected TTS connection"):
                jobs.create(imported.book_id, profile_id, other_provider_id)
            jobs.run(job_id)

            chunks = generation.list_chunks(job_id)
            self.assertEqual(generation.get_job(job_id)["status"], "completed")
            self.assertTrue(all(chunk.status == "completed" for chunk in chunks))
            self.assertEqual(len(fake.requests), len(chunks))
            self.assertTrue(all(request.reference_audio.is_file() for request in fake.requests))
            self.assertEqual(
                [request.seed for request in fake.requests],
                [
                    deterministic_chunk_seed(
                        imported.book_id, chunk.chapter_index, chunk.chunk_index
                    )
                    for chunk in chunks
                ],
            )

            jobs.run(job_id)
            self.assertEqual(len(fake.requests), len(chunks))

            corrupt_path = data / chunks[0].audio_artifact_path
            corrupt_path.write_bytes(b"corrupt")
            jobs.run(job_id)
            repaired = generation.list_chunks(job_id)[0]
            self.assertEqual(repaired.status, "completed")
            self.assertEqual(repaired.attempts, 2)
            self.assertEqual(len(fake.requests), len(chunks) + 1)

            generation.fail_chunk(job_id, repaired.database_id, "Previous attempt failed")
            self.assertEqual(
                generation.get_job(job_id)["error_message"],
                "Previous attempt failed",
            )
            requests_before_retry = len(fake.requests)
            jobs.prepare(job_id)
            prepared = generation.get_job(job_id)
            self.assertEqual(prepared["status"], "generating")
            self.assertIsNone(prepared["error_message"])
            jobs.run(job_id, prepared=True)
            self.assertEqual(generation.get_job(job_id)["status"], "completed")
            self.assertEqual(len(fake.requests), requests_before_retry + 1)

            alternate_profile_id = profiles.create_openmoss_profile(
                provider_id=provider_id,
                reference_audio=reference,
                instruction="A distinctly different narrator.",
                name="Alternate narrator",
            )
            alternate_job_id = jobs.create(imported.book_id, alternate_profile_id)
            alternate_chunks = generation.list_chunks(alternate_job_id)
            self.assertEqual(generation.get_job(alternate_job_id)["status"], "ready")
            self.assertTrue(all(chunk.status == "pending" for chunk in alternate_chunks))
            self.assertTrue(
                all(chunk.audio_artifact_path is None for chunk in alternate_chunks)
            )
            requests_before_alternate_run = len(fake.requests)
            jobs.run(alternate_job_id)
            self.assertEqual(
                len(fake.requests), requests_before_alternate_run + len(alternate_chunks)
            )
            self.assertTrue(
                {
                    chunk.audio_artifact_path
                    for chunk in generation.list_chunks(job_id)
                }.isdisjoint(
                    {
                        chunk.audio_artifact_path
                        for chunk in generation.list_chunks(alternate_job_id)
                    }
                )
            )

            ReviseNarrationPlan(books, layout, store).execute(imported.book_id, {2})
            revised_job_id = jobs.create(imported.book_id, profile_id)
            revised_chunks = generation.list_chunks(revised_job_id)
            requests_before_revised_run = len(fake.requests)

            self.assertEqual([chunk.id for chunk in revised_chunks], ["c0002-p0001"])
            self.assertEqual(revised_chunks[0].status, "pending")
            self.assertEqual(revised_chunks[0].attempts, 0)
            jobs.run(revised_job_id)
            self.assertEqual(len(fake.requests), requests_before_revised_run + 1)

    def test_regenerates_only_the_selected_chunk_and_preserves_the_old_audio_on_failure(
        self,
    ) -> None:
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
            profile_id = profiles.create_openmoss_profile(
                provider_id=provider_id,
                reference_audio=reference,
                instruction="A steady narrator.",
                name="Steady narrator",
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
            job_id = jobs.create(imported.book_id, profile_id)
            jobs.run(job_id)
            chunks = generation.list_chunks(job_id)
            selected = chunks[0]
            untouched = chunks[1]

            jobs.regenerate_chunk(job_id, selected.database_id)

            regenerated = generation.get_chunk(job_id, selected.database_id)
            self.assertEqual(generation.get_job(job_id)["status"], "completed")
            self.assertEqual(regenerated.status, "completed")
            self.assertEqual(regenerated.attempts, 2)
            self.assertEqual(
                generation.get_chunk(job_id, untouched.database_id).attempts,
                1,
            )
            self.assertEqual(len(fake.requests), len(chunks) + 1)

            audio_path = data / regenerated.audio_artifact_path
            previous_audio = audio_path.read_bytes()
            fake.failure_message = "The retry was rejected"
            with self.assertRaisesRegex(RuntimeError, "retry was rejected"):
                jobs.regenerate_chunk(job_id, selected.database_id)

            preserved = generation.get_chunk(job_id, selected.database_id)
            failed_job = generation.get_job(job_id)
            self.assertEqual(preserved.status, "completed")
            self.assertEqual(preserved.attempts, 3)
            self.assertEqual(audio_path.read_bytes(), previous_audio)
            self.assertEqual(failed_job["status"], "failed")
            self.assertEqual(failed_job["error_message"], "The retry was rejected")

            fake.failure_message = None
            jobs.regenerate_chunk(job_id, selected.database_id)
            retried_job = generation.get_job(job_id)
            self.assertEqual(retried_job["status"], "completed")
            self.assertIsNone(retried_job["error_message"])
            self.assertEqual(
                generation.get_chunk(job_id, selected.database_id).attempts,
                4,
            )
            selected_seed = deterministic_chunk_seed(
                imported.book_id, selected.chapter_index, selected.chunk_index
            )
            self.assertEqual(fake.requests[0].seed, selected_seed)
            self.assertTrue(
                all(request.seed == selected_seed for request in fake.requests[len(chunks):])
            )

    def test_legacy_zero_attempt_audio_is_removed_and_job_is_reopened(self) -> None:
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
            profile_id = profiles.create_openmoss_profile(
                provider_id=provider_id,
                reference_audio=reference,
                instruction="A fresh narrator.",
                name="Fresh narrator",
            )
            jobs = GenerationJobs(
                books, generation, layout, store, masters=FakeAudioMasters()
            )
            job_id = jobs.create(imported.book_id, profile_id)
            chunk = generation.list_chunks(job_id)[0]
            copied_audio = layout.job_chunk_master(imported.book_id, job_id, chunk.id)
            make_wave(copied_audio)
            generation.complete_chunk(
                job_id,
                chunk.database_id,
                copied_audio.relative_to(data).as_posix(),
                store.sha256(copied_audio),
                0.01,
            )
            generation.complete_job(job_id)

            GenerationJobs(
                books, generation, layout, store, masters=FakeAudioMasters()
            )

            repaired = generation.list_chunks(job_id)[0]
            self.assertEqual(generation.get_job(job_id)["status"], "ready")
            self.assertEqual(repaired.status, "pending")
            self.assertIsNone(repaired.audio_artifact_path)
            self.assertFalse(copied_audio.exists())

    def test_interrupted_chunk_returns_to_pending_before_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            database = Database(data / "narranova.sqlite3")
            database.initialize()
            generation = GenerationRepository(database)
            with database.connect() as connection:
                connection.execute(
                    "INSERT INTO provider_instances(id, kind, name, endpoint_url) "
                    "VALUES ('p', 'openmoss', 'MOSS', 'http://moss/tts')"
                )
                connection.execute(
                    "INSERT INTO books(id, source_sha256, source_artifact_path) "
                    "VALUES ('b', 'hash', 'source.epub')"
                )
                connection.execute(
                    "INSERT INTO narrator_profiles(id, provider_instance_id, "
                    "profile_json, profile_sha256) VALUES ('v', 'p', '{}', 'hash')"
                )
                connection.execute(
                    "INSERT INTO jobs(id, book_id, narrator_profile_id, status) "
                    "VALUES ('j', 'b', 'v', 'generating')"
                )
                connection.execute(
                    "INSERT INTO chunks(id, logical_id, job_id, chapter_index, chunk_index, "
                    "text_sha256, text_artifact_path, status) "
                    "VALUES ('j-c', 'c', 'j', 1, 1, 'hash', 'text.txt', 'generating')"
                )

            generation.recover_interrupted("j")

            self.assertEqual(generation.get_job("j")["status"], "ready")
            self.assertEqual(generation.list_chunks("j")[0].status, "pending")


if __name__ == "__main__":
    unittest.main()
