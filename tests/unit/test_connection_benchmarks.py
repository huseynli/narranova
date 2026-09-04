from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from narranova.application.benchmarking import (
    BENCHMARK_SEED,
    BENCHMARK_TEXT,
    BENCHMARK_VOICE_PAIR_ID,
    ConnectionBenchmarks,
    recommend_stream_chunk_frames,
)
from narranova.application.generation import VoiceProfiles
from narranova.artifacts import ArtifactLayout, ArtifactStore
from narranova.persistence import Database
from narranova.persistence.benchmarks import BenchmarkRepository
from narranova.persistence.generation import GenerationRepository
from narranova.providers import OPENMOSS_STREAM_FRAME_OPTIONS
from narranova.providers.base import SynthesisRequest, SynthesisResult
from tests.unit.test_generation_jobs import FakeAudioMasters, make_wave


class ControlledBenchmarkProvider:
    def __init__(self, frames: int, speed: float) -> None:
        self.frames = frames
        self.speed = speed
        self.requests: list[SynthesisRequest] = []

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        self.requests.append(request)
        make_wave(request.destination, frames=24_000)
        digest = hashlib.sha256(request.destination.read_bytes()).hexdigest()
        return SynthesisResult(
            request.destination.resolve(),
            digest,
            1.0,
            usage={
                "wall_seconds": 1 / self.speed,
                "first_audio_seconds": self.frames * 0.0005,
            },
        )


class ConnectionBenchmarkTests(unittest.TestCase):
    def test_recommendation_picks_smallest_batch_near_fastest(self) -> None:
        measurements = [
            {"stream_chunk_frames": 16, "realtime_speed": 2.0},
            {"stream_chunk_frames": 32, "realtime_speed": 2.8},
            {"stream_chunk_frames": 64, "realtime_speed": 3.4},
            {"stream_chunk_frames": 128, "realtime_speed": 3.60},
            {"stream_chunk_frames": 256, "realtime_speed": 3.64},
            {"stream_chunk_frames": 512, "realtime_speed": 3.65},
        ]

        self.assertEqual(recommend_stream_chunk_frames(measurements), 128)
        self.assertIsNone(recommend_stream_chunk_frames([]))

    def test_auto_tune_uses_fixed_inputs_and_persists_recommendation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            layout = ArtifactLayout.at(data)
            layout.initialize()
            store = ArtifactStore(data)
            database = Database(data / "narranova.sqlite3")
            database.initialize()
            generation = GenerationRepository(database)
            profiles = VoiceProfiles(generation, layout, store)
            provider_id = profiles.add_openmoss_provider(
                "Benchmark MOSS", "http://moss.test:8000/tts"
            )
            repository = BenchmarkRepository(database)
            speeds = {
                16: 2.0,
                32: 2.8,
                64: 3.4,
                128: 3.60,
                256: 3.64,
                512: 3.65,
            }
            providers: list[ControlledBenchmarkProvider] = []

            def provider_factory(provider, frames):
                controlled = ControlledBenchmarkProvider(frames, speeds[frames])
                providers.append(controlled)
                return controlled

            benchmarks = ConnectionBenchmarks(
                repository,
                generation,
                layout,
                store,
                provider_factory=provider_factory,
                masters=FakeAudioMasters(),
            )

            benchmark_id = benchmarks.create(provider_id)
            benchmarks.run(benchmark_id)
            run = repository.get_run(benchmark_id)

            self.assertEqual(run.status, "completed")
            self.assertEqual(run.requested_frames, OPENMOSS_STREAM_FRAME_OPTIONS)
            self.assertEqual(len(run.results), 6)
            self.assertEqual(run.recommended_stream_chunk_frames, 128)
            self.assertEqual([provider.frames for provider in providers], list(OPENMOSS_STREAM_FRAME_OPTIONS))
            requests = [provider.requests[0] for provider in providers]
            self.assertTrue(all(request.text == BENCHMARK_TEXT for request in requests))
            self.assertTrue(all(request.seed == BENCHMARK_SEED for request in requests))
            self.assertTrue(all(request.parameters == {} for request in requests))
            self.assertTrue(all(request.reference_audio.is_file() for request in requests))
            self.assertEqual(run.voice_pair_id, BENCHMARK_VOICE_PAIR_ID)
            self.assertTrue(all(result["estimated_40h_tts_hours"] for result in run.results))

            applied = benchmarks.apply(benchmark_id)
            configuration = generation.get_provider(provider_id)["configuration"]
            self.assertEqual(applied, 128)
            self.assertEqual(configuration["stream_chunk_frames"], 128)
            self.assertEqual(configuration["recommended_stream_chunk_frames"], 128)

            benchmark_root = layout.connection_benchmark_root(provider_id, benchmark_id)
            self.assertTrue(benchmark_root.is_dir())
            benchmarks.delete(provider_id, benchmark_id)
            self.assertFalse(benchmark_root.exists())
            with self.assertRaises(KeyError):
                repository.get_run(benchmark_id)

    def test_rejects_unsupported_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            layout = ArtifactLayout.at(data)
            layout.initialize()
            store = ArtifactStore(data)
            database = Database(data / "narranova.sqlite3")
            database.initialize()
            generation = GenerationRepository(database)
            profiles = VoiceProfiles(generation, layout, store)
            provider_id = profiles.add_openmoss_provider(
                "Benchmark MOSS", "http://moss.test:8000/tts"
            )
            benchmarks = ConnectionBenchmarks(
                BenchmarkRepository(database), generation, layout, store
            )

            with self.assertRaisesRegex(ValueError, "supported"):
                benchmarks.create(provider_id, 24)


if __name__ == "__main__":
    unittest.main()
