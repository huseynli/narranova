"""Controlled, book-independent performance benchmarks for TTS connections."""

from __future__ import annotations

import hashlib
import shutil
import time
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol, cast

from narranova.application.default_voices import default_voice_pair
from narranova.artifacts import ArtifactLayout, ArtifactStore
from narranova.audio import AudioMasterInfo, FFmpegAudioMasters, validate_wave
from narranova.persistence.benchmarks import BenchmarkRepository, StoredBenchmarkRun
from narranova.persistence.generation import GenerationRepository
from narranova.providers import (
    OPENMOSS_DEFAULT_MAX_NEW_TOKENS,
    OPENMOSS_STREAM_FRAME_OPTIONS,
    OpenMossConfig,
    OpenMossProvider,
    SynthesisRequest,
    TTSProvider,
)


BENCHMARK_VOICE_PAIR_ID = "builtin:04"
BENCHMARK_SEED = 1_904_117
BENCHMARK_TEXT = (
    "At first light, the station clock struck six, and the narrow streets began "
    "to stir. A baker lifted the shutters across the square while delivery carts "
    "rattled over the old stones. Mara waited beneath the awning with a folded map "
    "in one hand and a brass key in the other. The note had promised an answer, but "
    "it had not explained the question.\n\n"
    "She crossed the square slowly. Rainwater shone along the gutters, reflecting "
    "windows, chimneys, and a pale strip of morning sky. At number seventeen, a blue "
    "door stood between a tailor's shop and a silent café. The key turned without "
    "resistance. Inside, a staircase climbed toward a room filled with books, rolled "
    "charts, and small wooden boxes marked with dates.\n\n"
    "On the desk lay a single envelope. Mara recognized her grandfather's careful "
    "handwriting before she read her own name. She sat, listened to the building "
    "settle around her, and opened it. The letter described a journey he had never "
    "taken and a promise he had kept for thirty years. By the final page, the square "
    "outside was bright with voices. Mara read the last sentence twice, placed the "
    "map beside the letter, and smiled. Her train left at noon."
)
BENCHMARK_TEXT_SHA256 = hashlib.sha256(BENCHMARK_TEXT.encode("utf-8")).hexdigest()


class AudioMasters(Protocol):
    def normalize(self, source: Path, destination: Path) -> AudioMasterInfo: ...

    def validate(self, path: Path) -> AudioMasterInfo: ...


ProviderFactory = Callable[[Mapping[str, object], int], TTSProvider]


def recommend_stream_chunk_frames(
    measurements: Iterable[Mapping[str, object]], tolerance: float = 0.03
) -> int | None:
    """Choose the smallest supported batch within tolerance of peak throughput."""

    if not 0 <= tolerance < 1:
        raise ValueError("Recommendation tolerance must be between 0 and 1")
    speeds: dict[int, float] = {}
    for measurement in measurements:
        try:
            frames = int(cast(int | str, measurement["stream_chunk_frames"]))
            speed = float(cast(float | int | str, measurement["realtime_speed"]))
        except (KeyError, TypeError, ValueError):
            continue
        if frames not in OPENMOSS_STREAM_FRAME_OPTIONS or speed <= 0:
            continue
        speeds[frames] = max(speed, speeds.get(frames, 0.0))
    if not speeds:
        return None
    threshold = max(speeds.values()) * (1 - tolerance)
    return min(frames for frames, speed in speeds.items() if speed >= threshold)


class ConnectionBenchmarks:
    def __init__(
        self,
        repository: BenchmarkRepository,
        generation: GenerationRepository,
        layout: ArtifactLayout,
        store: ArtifactStore,
        provider_factory: ProviderFactory | None = None,
        masters: AudioMasters | None = None,
    ) -> None:
        self.repository = repository
        self.generation = generation
        self.layout = layout
        self.store = store
        self.provider_factory = provider_factory or self._openmoss_provider
        self.masters = masters or FFmpegAudioMasters()

    def create(self, provider_id: str, frames: int | None = None) -> str:
        provider = self.generation.get_provider(provider_id)
        if provider["kind"] != "openmoss" or not provider["enabled"]:
            raise ValueError("Choose an enabled OpenMOSS connection")
        if frames is None:
            mode = "auto"
            requested = OPENMOSS_STREAM_FRAME_OPTIONS
        else:
            if frames not in OPENMOSS_STREAM_FRAME_OPTIONS:
                raise ValueError("Choose a supported streaming decode batch")
            mode = "single"
            requested = (frames,)
        return self.repository.create_run(
            provider_id=provider_id,
            mode=mode,
            requested_frames=requested,
            benchmark_text_sha256=BENCHMARK_TEXT_SHA256,
            voice_pair_id=BENCHMARK_VOICE_PAIR_ID,
            seed=BENCHMARK_SEED,
            max_new_tokens=OPENMOSS_DEFAULT_MAX_NEW_TOKENS,
        )

    def run(self, benchmark_id: str) -> None:
        run = self.repository.get_run(benchmark_id)
        provider = self.generation.get_provider(run.provider_id)
        pair = default_voice_pair(BENCHMARK_VOICE_PAIR_ID)
        owner_id = f"benchmark-{benchmark_id}"
        claimed = False
        try:
            self.generation.claim_provider_work(run.provider_id, owner_id)
            claimed = True
            for frames in run.requested_frames:
                self.generation.renew_provider_work(owner_id)
                self.repository.set_active_frame(benchmark_id, frames)
                temporary = self.layout.connection_benchmark_temporary(
                    benchmark_id, frames
                )
                sample = self.layout.connection_benchmark_sample(
                    run.provider_id, benchmark_id, frames
                )
                started = time.monotonic()
                measurement_saved = False
                try:
                    result = self.provider_factory(provider, frames).synthesize(
                        SynthesisRequest(
                            text=BENCHMARK_TEXT,
                            destination=temporary,
                            language="English",
                            instruction=pair.instruction,
                            reference_audio=pair.audio_path,
                            seed=run.seed,
                        )
                    )
                    if result.audio_path.resolve() != temporary.resolve():
                        raise RuntimeError("TTS provider returned an unexpected benchmark path")
                    validate_wave(temporary)
                    if self.store.sha256(temporary) != result.audio_sha256:
                        raise RuntimeError("Benchmark audio failed hash validation")
                    master = self.masters.normalize(temporary, sample)
                    wall_seconds = self._positive_metric(
                        result.usage.get("wall_seconds"), time.monotonic() - started
                    )
                    audio_seconds = float(master.duration_seconds)
                    if audio_seconds <= 0:
                        raise RuntimeError("Benchmark generated no playable audio")
                    realtime_speed = audio_seconds / wall_seconds
                    first_audio = self._optional_metric(
                        result.usage.get("first_audio_seconds")
                    )
                    self.repository.append_result(
                        benchmark_id,
                        {
                            "stream_chunk_frames": frames,
                            "audio_duration_seconds": audio_seconds,
                            "wall_seconds": wall_seconds,
                            "realtime_speed": realtime_speed,
                            "realtime_factor": wall_seconds / audio_seconds,
                            "first_audio_seconds": first_audio,
                            "estimated_40h_tts_hours": 40 / realtime_speed,
                            "audio_artifact_path": sample.relative_to(
                                self.layout.root
                            ).as_posix(),
                            "audio_sha256": self.store.sha256(sample),
                        },
                    )
                    measurement_saved = True
                finally:
                    temporary.unlink(missing_ok=True)
                    if not measurement_saved:
                        sample.unlink(missing_ok=True)
            completed = self.repository.get_run(benchmark_id)
            recommendation = recommend_stream_chunk_frames(completed.results)
            if recommendation is None:
                raise RuntimeError("Benchmark did not produce a valid measurement")
            self.repository.complete_run(benchmark_id, recommendation)
            self._prune(run.provider_id)
        except Exception as exc:
            current = self.repository.get_run(benchmark_id)
            if current.status == "running":
                self.repository.fail_run(benchmark_id, str(exc))
            self._prune(run.provider_id)
            raise
        finally:
            if claimed:
                self.generation.release_provider_work(owner_id)

    def apply(self, benchmark_id: str, frames: int | None = None) -> int:
        run = self.repository.get_run(benchmark_id)
        if run.status != "completed" or run.recommended_stream_chunk_frames is None:
            raise ValueError("Complete the benchmark before applying its settings")
        selected = frames or run.recommended_stream_chunk_frames
        measured = {
            int(cast(int | str, result["stream_chunk_frames"]))
            for result in run.results
            if "stream_chunk_frames" in result
        }
        if selected not in measured or selected not in OPENMOSS_STREAM_FRAME_OPTIONS:
            raise ValueError("Choose a streaming decode batch measured by this benchmark")
        self.repository.apply_frames(
            provider_id=run.provider_id,
            selected_frames=selected,
            recommended_frames=run.recommended_stream_chunk_frames,
            benchmark_id=run.id,
        )
        return selected

    def delete(self, provider_id: str, benchmark_id: str) -> None:
        run = self.repository.get_run(benchmark_id)
        if run.provider_id != provider_id:
            raise KeyError("Connection benchmark not found")
        self.repository.delete_run(benchmark_id)
        shutil.rmtree(
            self.layout.connection_benchmark_root(provider_id, benchmark_id),
            ignore_errors=True,
        )

    def verified_sample(self, benchmark_id: str, frames: int) -> Path:
        run = self.repository.get_run(benchmark_id)
        result = next(
            (
                item
                for item in run.results
                if int(cast(int | str, item.get("stream_chunk_frames", -1))) == frames
            ),
            None,
        )
        if result is None:
            raise KeyError("Benchmark sample not found")
        path = self._artifact(str(result["audio_artifact_path"]))
        self.masters.validate(path)
        if self.store.sha256(path) != result["audio_sha256"]:
            raise RuntimeError("Benchmark sample failed hash validation")
        return path

    def _prune(self, provider_id: str) -> None:
        for old in self.repository.prune(provider_id, keep=5):
            shutil.rmtree(
                self.layout.connection_benchmark_root(provider_id, old.id),
                ignore_errors=True,
            )

    def _artifact(self, relative_path: str) -> Path:
        path = (self.layout.root / relative_path).resolve()
        if not path.is_relative_to(self.layout.root):
            raise RuntimeError("Stored artifact path escapes the data directory")
        return path

    @staticmethod
    def _positive_metric(value: object, fallback: float) -> float:
        try:
            number = float(cast(float | int | str, value))
        except (TypeError, ValueError):
            number = fallback
        return number if number > 0 else max(fallback, 0.000_001)

    @staticmethod
    def _optional_metric(value: object) -> float | None:
        if value is None:
            return None
        try:
            number = float(cast(float | int | str, value))
        except (TypeError, ValueError):
            return None
        return number if number >= 0 else None

    @staticmethod
    def _openmoss_provider(
        provider: Mapping[str, object], frames: int
    ) -> TTSProvider:
        if provider["kind"] != "openmoss":
            raise ValueError(f"Unsupported provider kind: {provider['kind']}")
        return OpenMossProvider(
            OpenMossConfig(
                str(provider["endpoint_url"]),
                max_new_tokens=OPENMOSS_DEFAULT_MAX_NEW_TOKENS,
                stream_chunk_frames=frames,
            )
        )
