from __future__ import annotations

import hashlib
import tempfile
import unittest
import wave
from pathlib import Path

from narranova.application.generation import GenerationJobs, VoiceProfiles
from narranova.application.ingest import ImportBook
from narranova.artifacts import ArtifactLayout, ArtifactStore
from narranova.epub import EpubParser
from narranova.persistence import Database
from narranova.persistence.books import BookRepository
from narranova.persistence.generation import GenerationRepository
from narranova.providers.base import SynthesisRequest, SynthesisResult
from test_epub_ingest import make_epub


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

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        self.requests.append(request)
        make_wave(request.destination)
        digest = hashlib.sha256(request.destination.read_bytes()).hexdigest()
        return SynthesisResult(request.destination, digest, 0.01)


class GenerationJobTests(unittest.TestCase):
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
                book_id=imported.book_id,
                provider_id=provider_id,
                reference_audio=reference,
                instruction="A careful narrator.",
            )
            fake = FakeProvider()
            jobs = GenerationJobs(
                books, generation, layout, store, provider_factory=lambda job: fake
            )

            job_id = jobs.create(imported.book_id, profile_id)
            jobs.run(job_id)

            chunks = generation.list_chunks(job_id)
            self.assertEqual(generation.get_job(job_id)["status"], "completed")
            self.assertTrue(all(chunk.status == "completed" for chunk in chunks))
            self.assertEqual(len(fake.requests), len(chunks))
            self.assertTrue(all(request.reference_audio.is_file() for request in fake.requests))

            jobs.run(job_id)
            self.assertEqual(len(fake.requests), len(chunks))

            corrupt_path = data / chunks[0].audio_artifact_path
            corrupt_path.write_bytes(b"corrupt")
            jobs.run(job_id)
            repaired = generation.list_chunks(job_id)[0]
            self.assertEqual(repaired.status, "completed")
            self.assertEqual(repaired.attempts, 2)
            self.assertEqual(len(fake.requests), len(chunks) + 1)

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
                    "INSERT INTO voice_profiles(id, book_id, provider_instance_id, "
                    "profile_json, profile_sha256) VALUES ('v', 'b', 'p', '{}', 'hash')"
                )
                connection.execute(
                    "INSERT INTO jobs(id, book_id, voice_profile_id, status) "
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
