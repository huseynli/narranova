"""In-process thread launchers; durable exclusivity lives in SQLite leases."""

from __future__ import annotations

import threading

from narranova.application.assembly import AudioAssembler
from narranova.application.benchmarking import ConnectionBenchmarks
from narranova.application.generation import GenerationJobs
from narranova.application.voice_studio import VoiceStudio


class JobSupervisor:
    def __init__(self, jobs: GenerationJobs, assembler: AudioAssembler) -> None:
        self.jobs = jobs
        self.assembler = assembler
        self._lock = threading.Lock()
        self._active: dict[str, str] = {}

    def start(self, job_id: str) -> bool:
        return self._launch(job_id, "job", self.jobs.prepare, self._run, job_id)

    def regenerate(self, job_id: str, chunk_id: str) -> bool:
        return self._launch(
            job_id,
            "chunk",
            lambda: self.jobs.prepare_chunk_regeneration(job_id, chunk_id),
            self._regenerate,
            job_id,
            chunk_id,
        )

    def assemble(self, job_id: str) -> bool:
        return self._launch(
            job_id, "assembly", self.assembler.prepare, self._assemble, job_id
        )

    def _launch(self, key: str, kind: str, prepare, target, *args: str) -> bool:
        with self._lock:
            if key in self._active:
                return False
            self._active[key] = kind
        try:
            prepare(*(() if kind == "chunk" else (key,)))
        except Exception:
            with self._lock:
                self._active.pop(key, None)
            raise
        threading.Thread(target=target, args=args, daemon=True).start()
        return True

    def is_active(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._active

    def is_regenerating(self, job_id: str) -> bool:
        with self._lock:
            return self._active.get(job_id) == "chunk"

    def is_assembling(self, job_id: str) -> bool:
        with self._lock:
            return self._active.get(job_id) == "assembly"

    def _run(self, job_id: str) -> None:
        self._guard(job_id, self.jobs.run, job_id, prepared=True)

    def _regenerate(self, job_id: str, chunk_id: str) -> None:
        self._guard(
            job_id, self.jobs.regenerate_chunk, job_id, chunk_id, prepared=True
        )

    def _assemble(self, job_id: str) -> None:
        self._guard(job_id, self.assembler.run, job_id, prepared=True)

    def _guard(self, key: str, operation, *args: str, **kwargs: object) -> None:
        try:
            operation(*args, **kwargs)
        except Exception:
            # Application services persist failures for the status endpoint.
            pass
        finally:
            with self._lock:
                self._active.pop(key, None)


class BenchmarkSupervisor:
    def __init__(self, benchmarks: ConnectionBenchmarks) -> None:
        self.benchmarks = benchmarks
        self._lock = threading.Lock()
        self._active: set[str] = set()

    def start(self, benchmark_id: str) -> None:
        with self._lock:
            if benchmark_id in self._active:
                raise ValueError("Benchmark is already running")
            self._active.add(benchmark_id)
        threading.Thread(target=self._run, args=(benchmark_id,), daemon=True).start()

    def _run(self, benchmark_id: str) -> None:
        try:
            self.benchmarks.run(benchmark_id)
        except Exception:
            pass
        finally:
            with self._lock:
                self._active.discard(benchmark_id)


class VoiceStudioSupervisor:
    def __init__(self, studio: VoiceStudio) -> None:
        self.studio = studio
        self._lock = threading.Lock()
        self._active: set[str] = set()

    def start(self, draft_id: str, **arguments: object) -> bool:
        with self._lock:
            if draft_id in self._active:
                return False
            self._active.add(draft_id)
        self.studio.mark_audition_queued(draft_id)
        threading.Thread(
            target=self._run, args=(draft_id, arguments), daemon=True
        ).start()
        return True

    def is_active(self, draft_id: str) -> bool:
        with self._lock:
            return draft_id in self._active

    def _run(self, draft_id: str, arguments: dict[str, object]) -> None:
        try:
            self.studio.generate_take(draft_id, **arguments)
        except Exception:
            pass
        finally:
            with self._lock:
                self._active.discard(draft_id)
