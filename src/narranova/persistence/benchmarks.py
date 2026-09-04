"""Persistence for controlled connection-performance benchmark runs."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from sqlite3 import Row
from typing import Mapping

from narranova.persistence.database import Database


@dataclass(frozen=True)
class StoredBenchmarkRun:
    id: str
    provider_id: str
    mode: str
    status: str
    requested_frames: tuple[int, ...]
    active_stream_chunk_frames: int | None
    benchmark_text_sha256: str
    voice_pair_id: str
    seed: int
    max_new_tokens: int
    results: tuple[dict[str, object], ...]
    recommended_stream_chunk_frames: int | None
    error_message: str | None
    created_at: str
    completed_at: str | None


class BenchmarkRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create_run(
        self,
        *,
        provider_id: str,
        mode: str,
        requested_frames: tuple[int, ...],
        benchmark_text_sha256: str,
        voice_pair_id: str,
        seed: int,
        max_new_tokens: int,
    ) -> str:
        if mode not in {"single", "auto"}:
            raise ValueError("Benchmark mode must be single or auto")
        benchmark_id = uuid.uuid4().hex
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT 1 FROM connection_benchmark_runs "
                "WHERE provider_instance_id = ? AND status = 'running'",
                (provider_id,),
            ).fetchone()
            if existing is not None:
                raise ValueError("A benchmark is already running for this connection")
            connection.execute(
                """
                INSERT INTO connection_benchmark_runs(
                    id, provider_instance_id, mode, status,
                    requested_frames_json, benchmark_text_sha256,
                    voice_pair_id, seed, max_new_tokens
                ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?)
                """,
                (
                    benchmark_id,
                    provider_id,
                    mode,
                    json.dumps(requested_frames),
                    benchmark_text_sha256,
                    voice_pair_id,
                    seed,
                    max_new_tokens,
                ),
            )
        return benchmark_id

    def get_run(self, benchmark_id: str) -> StoredBenchmarkRun:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM connection_benchmark_runs WHERE id = ?",
                (benchmark_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Connection benchmark not found: {benchmark_id}")
        return self._stored_run(row)

    def list_runs(self, provider_id: str, limit: int = 5) -> list[StoredBenchmarkRun]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM connection_benchmark_runs
                WHERE provider_instance_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (provider_id, limit),
            ).fetchall()
        return [self._stored_run(row) for row in rows]

    def delete_run(self, benchmark_id: str) -> StoredBenchmarkRun:
        run = self.get_run(benchmark_id)
        if run.status == "running":
            raise ValueError("A running benchmark cannot be deleted")
        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM connection_benchmark_runs "
                "WHERE id = ? AND status != 'running'",
                (benchmark_id,),
            )
        if cursor.rowcount != 1:
            raise ValueError("Benchmark could not be deleted")
        return run

    def set_active_frame(self, benchmark_id: str, frames: int) -> None:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE connection_benchmark_runs
                SET active_stream_chunk_frames = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'running'
                """,
                (frames, benchmark_id),
            )
        if cursor.rowcount != 1:
            raise ValueError("Benchmark is no longer running")

    def append_result(self, benchmark_id: str, result: Mapping[str, object]) -> None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT results_json FROM connection_benchmark_runs "
                "WHERE id = ? AND status = 'running'",
                (benchmark_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Benchmark is no longer running")
            results = json.loads(row["results_json"])
            if not isinstance(results, list):
                raise RuntimeError("Stored benchmark results are corrupt")
            results.append(dict(result))
            connection.execute(
                """
                UPDATE connection_benchmark_runs
                SET results_json = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (json.dumps(results, sort_keys=True), benchmark_id),
            )

    def complete_run(self, benchmark_id: str, recommended_frames: int) -> None:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE connection_benchmark_runs
                SET status = 'completed', active_stream_chunk_frames = NULL,
                    recommended_stream_chunk_frames = ?, error_message = NULL,
                    completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'running'
                """,
                (recommended_frames, benchmark_id),
            )
        if cursor.rowcount != 1:
            raise ValueError("Benchmark is no longer running")

    def fail_run(self, benchmark_id: str, message: str) -> None:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE connection_benchmark_runs
                SET status = 'failed', active_stream_chunk_frames = NULL,
                    error_message = ?, completed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'running'
                """,
                (message, benchmark_id),
            )
        if cursor.rowcount != 1:
            raise ValueError("Benchmark is no longer running")

    def apply_frames(
        self,
        *,
        provider_id: str,
        selected_frames: int,
        recommended_frames: int,
        benchmark_id: str,
    ) -> None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT configuration_json FROM provider_instances WHERE id = ?",
                (provider_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"TTS connection not found: {provider_id}")
            configuration = json.loads(row["configuration_json"])
            if not isinstance(configuration, dict):
                raise RuntimeError("TTS connection configuration is corrupt")
            configuration.update(
                {
                    "stream_chunk_frames": selected_frames,
                    "recommended_stream_chunk_frames": recommended_frames,
                    "benchmark_id": benchmark_id,
                }
            )
            connection.execute(
                """
                UPDATE provider_instances
                SET configuration_json = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (json.dumps(configuration, sort_keys=True), provider_id),
            )

    def recover_interrupted(self) -> int:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE connection_benchmark_runs
                SET status = 'failed', active_stream_chunk_frames = NULL,
                    error_message = 'Benchmark interrupted when Narranova stopped',
                    completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE status = 'running' AND NOT EXISTS (
                    SELECT 1 FROM provider_work_leases lease
                    WHERE lease.provider_instance_id =
                          connection_benchmark_runs.provider_instance_id
                      AND lease.owner_id = 'benchmark-' || connection_benchmark_runs.id
                      AND lease.lease_expires_at >= ?
                )
                """,
                (time.time(),),
            )
        return cursor.rowcount

    def prune(self, provider_id: str, keep: int = 5) -> list[StoredBenchmarkRun]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM connection_benchmark_runs
                WHERE provider_instance_id = ? AND status != 'running'
                ORDER BY created_at DESC, rowid DESC
                LIMIT -1 OFFSET ?
                """,
                (provider_id, keep),
            ).fetchall()
            identifiers = [row["id"] for row in rows]
            connection.executemany(
                "DELETE FROM connection_benchmark_runs WHERE id = ?",
                [(identifier,) for identifier in identifiers],
            )
        return [self._stored_run(row) for row in rows]

    @staticmethod
    def _stored_run(row: Row) -> StoredBenchmarkRun:
        values = dict(row)
        requested = json.loads(values.pop("requested_frames_json"))
        results = json.loads(values.pop("results_json"))
        if not isinstance(requested, list) or not isinstance(results, list):
            raise RuntimeError("Stored benchmark data is corrupt")
        return StoredBenchmarkRun(
            id=str(values["id"]),
            provider_id=str(values["provider_instance_id"]),
            mode=str(values["mode"]),
            status=str(values["status"]),
            requested_frames=tuple(int(value) for value in requested),
            active_stream_chunk_frames=(
                int(values["active_stream_chunk_frames"])
                if values["active_stream_chunk_frames"] is not None
                else None
            ),
            benchmark_text_sha256=str(values["benchmark_text_sha256"]),
            voice_pair_id=str(values["voice_pair_id"]),
            seed=int(values["seed"]),
            max_new_tokens=int(values["max_new_tokens"]),
            results=tuple(dict(value) for value in results),
            recommended_stream_chunk_frames=(
                int(values["recommended_stream_chunk_frames"])
                if values["recommended_stream_chunk_frames"] is not None
                else None
            ),
            error_message=(
                str(values["error_message"])
                if values["error_message"] is not None
                else None
            ),
            created_at=str(values["created_at"]),
            completed_at=(
                str(values["completed_at"])
                if values["completed_at"] is not None
                else None
            ),
        )
