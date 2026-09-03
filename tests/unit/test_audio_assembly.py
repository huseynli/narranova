from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

from narranova.application.assembly import AudioAssembler
from narranova.application.generation import GenerationJobs, VoiceProfiles
from narranova.application.ingest import ImportBook
from narranova.artifacts import ArtifactLayout, ArtifactStore
from narranova.audio import FFmpegM4BEncoder, M4BChapter, assemble_wave, validate_wave
from narranova.epub import EpubParser
from narranova.persistence import Database
from narranova.persistence.books import BookRepository
from narranova.persistence.generation import GenerationRepository
from tests.unit.test_epub_ingest import make_epub
from tests.unit.test_generation_jobs import FakeProvider, make_wave


class FakeEncoder:
    def __init__(self, error: str | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    def encode(self, chapters, destination, metadata, workspace, cover=None):
        self.calls.append(
            {
                "chapters": chapters,
                "destination": destination,
                "metadata": metadata,
                "cover": cover,
            }
        )
        if self.error:
            raise RuntimeError(self.error)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"Narranova test M4B")
        return SimpleNamespace(duration_seconds=chapters[-1].end_seconds)


def assembly_workspace(root: Path, encoder: FakeEncoder):
    source = root / "book.epub"
    reference = root / "reference.wav"
    make_epub(source)
    make_wave(reference)
    data = root / "data"
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
        instruction="A clear narrator.",
        name="Clear narrator",
    )
    provider = FakeProvider()
    jobs = GenerationJobs(
        books, generation, layout, store, provider_factory=lambda job: provider
    )
    job_id = jobs.create(imported.book_id, profile_id)
    jobs.run(job_id)
    assembler = AudioAssembler(generation, layout, store, encoder=encoder)
    return generation, layout, store, jobs, assembler, job_id


class WaveAssemblyTests(unittest.TestCase):
    def test_concatenates_compatible_waves_and_rejects_mismatched_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.wav"
            second = root / "second.wav"
            mismatch = root / "mismatch.wav"
            destination = root / "chapter.wav"
            make_wave(first, 120)
            make_wave(second, 240)
            make_wave(mismatch, 120)
            # Re-create the mismatch at a different sample rate.
            with wave.open(str(mismatch), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16_000)
                output.writeframes(b"\x01\x00" * 120)

            result = assemble_wave([first, second], destination)

            self.assertEqual(result.info.frames, 360)
            self.assertEqual(validate_wave(destination).sample_rate, 24_000)
            with self.assertRaisesRegex(ValueError, "sample_rate"):
                assemble_wave([first, mismatch], root / "invalid.wav")


class AudioAssemblerTests(unittest.TestCase):
    def test_builds_chapters_map_and_audiobook_then_invalidates_only_affected_outputs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            encoder = FakeEncoder()
            generation, layout, _, jobs, assembler, job_id = assembly_workspace(
                Path(temporary), encoder
            )

            result = assembler.run(job_id)

            artifacts = generation.list_job_artifacts(job_id)
            kinds = [artifact.kind for artifact in artifacts]
            self.assertEqual(kinds.count("chapter_audio"), 2)
            self.assertIn("audiobook", kinds)
            self.assertIn("narration_map", kinds)
            self.assertIn("cover", kinds)
            self.assertEqual(result.chapter_count, 2)
            self.assertTrue(result.audiobook_path.is_file())
            self.assertEqual(generation.get_job(job_id)["status"], "completed")
            self.assertEqual(encoder.calls[0]["metadata"]["title"], "The Example Book")
            self.assertIsNotNone(encoder.calls[0]["cover"])

            narration_map = json.loads(result.narration_map_path.read_text(encoding="utf-8"))
            self.assertEqual(narration_map["schema_version"], 1)
            self.assertEqual(narration_map["voice"]["name"], "Clear narrator")
            self.assertEqual(len(narration_map["chapters"]), 2)
            first_unit = narration_map["chapters"][0]["chunks"][0]["units"][0]
            self.assertIn("document", first_unit)
            self.assertIn("element_id", first_unit)

            selected = generation.list_chunks(job_id)[0]
            unaffected_index = generation.list_chunks(job_id)[-1].chapter_index
            jobs.regenerate_chunk(job_id, selected.database_id)

            remaining = generation.list_job_artifacts(job_id)
            self.assertNotIn("audiobook", [artifact.kind for artifact in remaining])
            self.assertNotIn("narration_map", [artifact.kind for artifact in remaining])
            remaining_chapters = {
                artifact.chapter_index
                for artifact in remaining
                if artifact.kind == "chapter_audio"
            }
            self.assertEqual(remaining_chapters, {unaffected_index})
            self.assertFalse(
                layout.job_chapter_audio(
                    generation.get_job(job_id)["book_id"],
                    job_id,
                    selected.chapter_index,
                ).exists()
            )

    def test_encoder_failure_is_durable_and_keeps_verified_chapter_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            encoder = FakeEncoder("FFmpeg is unavailable")
            generation, _, _, _, assembler, job_id = assembly_workspace(
                Path(temporary), encoder
            )

            with self.assertRaisesRegex(RuntimeError, "FFmpeg is unavailable"):
                assembler.run(job_id)

            job = generation.get_job(job_id)
            artifacts = generation.list_job_artifacts(job_id)
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["error_message"], "FFmpeg is unavailable")
            self.assertEqual(
                len([item for item in artifacts if item.kind == "chapter_audio"]),
                2,
            )
            self.assertTrue(any(item.kind == "narration_map" for item in artifacts))
            self.assertFalse(any(item.kind == "audiobook" for item in artifacts))

    def test_interrupted_assembly_becomes_retryable_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            encoder = FakeEncoder()
            generation, _, _, _, assembler, job_id = assembly_workspace(
                Path(temporary), encoder
            )
            assembler.prepare(job_id)

            recovered = generation.recover_interrupted_assemblies()

            self.assertEqual(recovered, 1)
            job = generation.get_job(job_id)
            self.assertEqual(job["status"], "failed")
            self.assertIn("interrupted", job["error_message"])
            result = assembler.run(job_id)
            self.assertTrue(result.audiobook_path.is_file())
            self.assertEqual(generation.get_job(job_id)["status"], "completed")


class M4BMetadataTests(unittest.TestCase):
    def test_writes_exact_chapter_boundaries_and_escapes_metadata(self) -> None:
        chapters = [
            M4BChapter("One; opening", Path("one.wav"), 0.0, 1.25),
            M4BChapter("Two", Path("two.wav"), 1.25, 3.0),
        ]

        metadata = FFmpegM4BEncoder._metadata(chapters, {"title": "A=B"})

        self.assertIn("title=A\\=B", metadata)
        self.assertIn("START=0\nEND=1250", metadata)
        self.assertIn("START=1250\nEND=3000", metadata)
        self.assertIn("title=One\\; opening", metadata)

    def test_encoder_builds_a_chapterized_cover_aware_ffmpeg_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ffmpeg = root / "ffmpeg"
            ffprobe = root / "ffprobe"
            ffmpeg.touch()
            ffprobe.touch()
            chapters = []
            for index in range(2):
                path = root / f"chapter-{index}.wav"
                make_wave(path)
                chapters.append(
                    M4BChapter(
                        f"Chapter {index + 1}",
                        path,
                        index * 0.01,
                        (index + 1) * 0.01,
                    )
                )
            cover = root / "cover.jpg"
            cover.write_bytes(b"test cover")
            commands: list[list[str]] = []

            def runner(command, **kwargs):
                commands.append(command)
                if command[0] == str(ffmpeg):
                    Path(command[-1]).write_bytes(b"encoded")
                    return subprocess.CompletedProcess(command, 0, "", "")
                return subprocess.CompletedProcess(
                    command, 0, json.dumps({"format": {"duration": "0.02"}}), ""
                )

            destination = root / "book.m4b"
            encoded = FFmpegM4BEncoder(
                str(ffmpeg), str(ffprobe), runner=runner
            ).encode(
                chapters,
                destination,
                {"title": "Test book"},
                root / "workspace",
                cover,
            )

            self.assertEqual(encoded.duration_seconds, 0.02)
            self.assertEqual(destination.read_bytes(), b"encoded")
            ffmpeg_command = commands[0]
            self.assertIn("-filter_complex", ffmpeg_command)
            self.assertTrue(
                any(
                    "concat=n=2:v=0:a=1[audiobook]" in argument
                    for argument in ffmpeg_command
                )
            )
            self.assertIn("-map_chapters", ffmpeg_command)
            self.assertIn("attached_pic", ffmpeg_command)
            self.assertIn("-f", ffmpeg_command)
            self.assertIn("ipod", ffmpeg_command)


if __name__ == "__main__":
    unittest.main()
