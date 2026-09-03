"""Provider, voice-profile, and durable generation-job persistence."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from narranova.application.planning import SynthesisChunk
from narranova.persistence.database import Database


@dataclass(frozen=True)
class StoredChunk:
    database_id: str
    id: str
    chapter_index: int
    chunk_index: int
    unit_ids: tuple[str, ...]
    status: str
    attempts: int
    text_sha256: str
    text_artifact_path: str
    audio_artifact_path: str | None
    audio_sha256: str | None
    duration_seconds: float | None


@dataclass(frozen=True)
class StoredProvider:
    id: str
    kind: str
    name: str
    endpoint_url: str
    enabled: int


@dataclass(frozen=True)
class StoredVoiceProfile:
    id: str
    provider_id: str
    provider_name: str
    provider_kind: str
    name: str
    profile: dict[str, Any]
    in_use_job_count: int


@dataclass(frozen=True)
class StoredJob:
    id: str
    book_id: str
    book_title: str
    status: str
    error_message: str | None
    created_at: str


@dataclass(frozen=True)
class UnattemptedCompletedChunk:
    job_id: str
    database_id: str
    audio_artifact_path: str


@dataclass(frozen=True)
class StoredArtifact:
    id: str
    kind: str
    relative_path: str
    sha256: str
    byte_size: int
    chapter_index: int | None
    metadata: dict[str, Any]


class GenerationRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def add_provider(self, kind: str, name: str, endpoint_url: str) -> str:
        provider_id = uuid.uuid4().hex
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO provider_instances(id, kind, name, endpoint_url)
                VALUES (?, ?, ?, ?)
                """,
                (provider_id, kind, name, endpoint_url),
            )
        return provider_id

    def add_openmoss_provider(self, name: str, endpoint_url: str) -> str:
        return self.add_provider("openmoss", name, endpoint_url)

    def list_providers(self) -> list[StoredProvider]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, kind, name, endpoint_url, enabled
                FROM provider_instances ORDER BY created_at, name
                """
            ).fetchall()
        return [StoredProvider(**dict(row)) for row in rows]

    def get_provider(self, provider_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT id, kind, name, endpoint_url, configuration_json, enabled
                FROM provider_instances WHERE id = ?
                """,
                (provider_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"TTS connection not found: {provider_id}")
        result = dict(row)
        result["configuration"] = json.loads(result.pop("configuration_json"))
        return result

    def update_provider(
        self,
        provider_id: str,
        *,
        kind: str,
        name: str,
        endpoint_url: str,
    ) -> None:
        with self.database.connect() as connection:
            current = connection.execute(
                "SELECT kind FROM provider_instances WHERE id = ?", (provider_id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"TTS connection not found: {provider_id}")
            if current["kind"] != kind:
                in_use = connection.execute(
                    "SELECT 1 FROM narrator_profiles "
                    "WHERE provider_instance_id = ? LIMIT 1",
                    (provider_id,),
                ).fetchone()
                if in_use:
                    raise ValueError(
                        "Create a new connection instead of changing the type of one "
                        "used by voice profiles"
                    )
            cursor = connection.execute(
                """
                UPDATE provider_instances
                SET kind = ?, name = ?, endpoint_url = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (kind, name, endpoint_url, provider_id),
            )
        if cursor.rowcount != 1:  # Defensive: the row was resolved in this transaction.
            raise RuntimeError("TTS connection changed during update")

    def delete_provider(self, provider_id: str) -> None:
        with self.database.connect() as connection:
            profile_in_use = connection.execute(
                "SELECT 1 FROM narrator_profiles WHERE provider_instance_id = ? LIMIT 1",
                (provider_id,),
            ).fetchone()
            job_in_use = connection.execute(
                "SELECT 1 FROM jobs WHERE provider_instance_id = ? LIMIT 1",
                (provider_id,),
            ).fetchone()
            benchmark_running = connection.execute(
                "SELECT 1 FROM connection_benchmark_runs "
                "WHERE provider_instance_id = ? AND status = 'running' LIMIT 1",
                (provider_id,),
            ).fetchone()
            if benchmark_running:
                raise ValueError("Wait for the connection benchmark to finish first")
            if profile_in_use or job_in_use:
                raise ValueError(
                    "Delete profiles and generation jobs using this TTS connection first"
                )
            cursor = connection.execute(
                "DELETE FROM provider_instances WHERE id = ?", (provider_id,)
            )
        if cursor.rowcount != 1:
            raise KeyError(f"TTS connection not found: {provider_id}")

    def list_voice_profiles(self) -> list[StoredVoiceProfile]:
        query = """
            SELECT v.id, p.id AS provider_id, p.name AS provider_name,
                   p.kind AS provider_kind, v.profile_json,
                   (
                       SELECT COUNT(*) FROM jobs j
                       WHERE j.narrator_profile_id = v.id
                         AND j.status != 'completed'
                   ) AS in_use_job_count
            FROM narrator_profiles v
            JOIN provider_instances p ON p.id = v.provider_instance_id
            ORDER BY v.updated_at DESC, v.id DESC
        """
        with self.database.connect() as connection:
            rows = connection.execute(query).fetchall()
        profiles = []
        for row in rows:
            profile = json.loads(row["profile_json"])
            profiles.append(
                StoredVoiceProfile(
                    id=row["id"],
                    provider_id=row["provider_id"],
                    provider_name=row["provider_name"],
                    provider_kind=row["provider_kind"],
                    name=str(profile.get("name") or "Untitled voice"),
                    profile=profile,
                    in_use_job_count=int(row["in_use_job_count"]),
                )
            )
        return profiles

    def list_jobs(self, book_id: str | None = None) -> list[StoredJob]:
        query = """
            SELECT j.id, j.book_id, COALESCE(b.title, 'Untitled') AS book_title,
                   j.status, j.error_message, j.created_at
            FROM jobs j JOIN books b ON b.id = j.book_id
        """
        parameters: tuple[str, ...] = ()
        if book_id is not None:
            query += " WHERE j.book_id = ?"
            parameters = (book_id,)
        query += " ORDER BY j.created_at DESC, j.id DESC"
        with self.database.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [StoredJob(**dict(row)) for row in rows]

    def get_chunk(self, job_id: str, chunk_id: str) -> StoredChunk:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT id AS database_id, logical_id AS id,
                       chapter_index, chunk_index, unit_ids_json,
                       status, attempts,
                       text_sha256, text_artifact_path, audio_artifact_path,
                       audio_sha256, duration_seconds
                FROM chunks WHERE job_id = ? AND id = ?
                """,
                (job_id, chunk_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"Chunk not found: {chunk_id}")
        return self._stored_chunk(row)

    def list_unattempted_completed_chunks(self) -> list[UnattemptedCompletedChunk]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT c.job_id, c.id AS database_id, c.audio_artifact_path
                FROM chunks c
                WHERE c.status = 'completed' AND c.attempts = 0
                  AND c.audio_artifact_path IS NOT NULL
                """,
            ).fetchall()
        return [UnattemptedCompletedChunk(**dict(row)) for row in rows]

    def add_voice_profile(
        self,
        *,
        profile_id: str,
        provider_id: str,
        profile: dict[str, Any],
        profile_sha256: str,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO narrator_profiles(
                    id, provider_instance_id, profile_json, profile_sha256
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    profile_id,
                    provider_id,
                    json.dumps(profile, ensure_ascii=False, sort_keys=True),
                    profile_sha256,
                ),
            )

    def get_voice_and_provider(self, profile_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT v.id, v.profile_json, v.profile_sha256,
                       p.id AS provider_id, p.kind AS provider_kind,
                       p.name AS provider_name, p.endpoint_url,
                       p.configuration_json, p.enabled
                FROM narrator_profiles v
                JOIN provider_instances p ON p.id = v.provider_instance_id
                WHERE v.id = ?
                """,
                (profile_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Voice profile not found: {profile_id}")
        result = dict(row)
        result["profile"] = json.loads(result.pop("profile_json"))
        result["provider_configuration"] = json.loads(result.pop("configuration_json"))
        return result

    def update_voice_profile(
        self,
        profile_id: str,
        *,
        provider_id: str,
        profile: dict[str, Any],
        profile_sha256: str,
    ) -> None:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE narrator_profiles
                SET provider_instance_id = ?, profile_json = ?, profile_sha256 = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    provider_id,
                    json.dumps(profile, ensure_ascii=False, sort_keys=True),
                    profile_sha256,
                    profile_id,
                ),
            )
        if cursor.rowcount != 1:
            raise KeyError(f"Voice profile not found: {profile_id}")

    def delete_voice_profile(self, profile_id: str) -> None:
        with self.database.connect() as connection:
            in_use = connection.execute(
                """
                SELECT COUNT(*) AS count FROM jobs
                WHERE narrator_profile_id = ? AND status != 'completed'
                """,
                (profile_id,),
            ).fetchone()
            if in_use and int(in_use["count"]) > 0:
                count = int(in_use["count"])
                noun = "job" if count == 1 else "jobs"
                raise ValueError(
                    f"Voice profile is in use by {count} unfinished generation {noun}"
                )
            connection.execute(
                "UPDATE jobs SET narrator_profile_id = NULL "
                "WHERE narrator_profile_id = ?",
                (profile_id,),
            )
            cursor = connection.execute(
                "DELETE FROM narrator_profiles WHERE id = ?", (profile_id,)
            )
        if cursor.rowcount != 1:
            raise KeyError(f"Voice profile not found: {profile_id}")

    def create_job(
        self,
        *,
        job_id: str,
        book_id: str,
        plan_id: str,
        voice_profile_id: str | None,
        provider_id: str,
        connection_configuration_snapshot: dict[str, Any],
        profile_snapshot: dict[str, Any],
        profile_snapshot_sha256: str,
        chunks: list[tuple[SynthesisChunk, str, str]],
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs(
                    id, book_id, narration_plan_id, narrator_profile_id,
                    provider_instance_id,
                    connection_configuration_snapshot_json,
                    voice_profile_snapshot_json, voice_profile_snapshot_sha256,
                    status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ready')
                """,
                (
                    job_id,
                    book_id,
                    plan_id,
                    voice_profile_id,
                    provider_id,
                    json.dumps(
                        connection_configuration_snapshot,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    json.dumps(profile_snapshot, ensure_ascii=False, sort_keys=True),
                    profile_snapshot_sha256,
                ),
            )
            connection.executemany(
                """
                INSERT INTO chunks(
                    id, job_id, chapter_index, chunk_index, text_sha256,
                    status, text_artifact_path, unit_ids_json, logical_id
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                [
                    (
                        f"{job_id}-{chunk.id}",
                        job_id,
                        chunk.chapter_index,
                        chunk.chunk_index,
                        text_hash,
                        text_path,
                        json.dumps(chunk.unit_ids),
                        chunk.id,
                    )
                    for chunk, text_path, text_hash in chunks
                ],
            )
            connection.execute(
                "UPDATE narration_plans SET locked_at = CURRENT_TIMESTAMP WHERE id = ?",
                (plan_id,),
            )

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT j.*, COALESCE(b.title, 'Untitled') AS book_title,
                       b.author AS book_author, b.language AS book_language,
                       b.source_artifact_path,
                       np.artifact_path AS plan_artifact_path,
                       np.plan_sha256, np.revision AS plan_revision,
                       COALESCE(j.voice_profile_snapshot_json, v.profile_json)
                           AS profile_json,
                       COALESCE(j.voice_profile_snapshot_sha256, v.profile_sha256)
                           AS profile_sha256,
                       p.name AS provider_name, p.kind AS provider_kind,
                       p.endpoint_url,
                       COALESCE(
                           j.connection_configuration_snapshot_json,
                           p.configuration_json
                       ) AS connection_configuration_json
                FROM jobs j
                JOIN books b ON b.id = j.book_id
                LEFT JOIN narration_plans np ON np.id = j.narration_plan_id
                LEFT JOIN narrator_profiles v ON v.id = j.narrator_profile_id
                JOIN provider_instances p
                  ON p.id = COALESCE(j.provider_instance_id, v.provider_instance_id)
                WHERE j.id = ?
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Generation job not found: {job_id}")
        result = dict(row)
        result["profile"] = json.loads(result.pop("profile_json"))
        result["provider_configuration"] = json.loads(
            result.pop("connection_configuration_json")
        )
        return result

    def list_job_voice_snapshots(self) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT j.id, j.book_id, j.narrator_profile_id,
                       j.voice_profile_snapshot_json,
                       j.voice_profile_snapshot_sha256,
                       v.profile_json AS current_profile_json
                FROM jobs j
                LEFT JOIN narrator_profiles v ON v.id = j.narrator_profile_id
                WHERE j.voice_profile_snapshot_json IS NOT NULL
                """
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["profile"] = json.loads(item.pop("voice_profile_snapshot_json"))
            current = item.pop("current_profile_json")
            item["current_profile"] = json.loads(current) if current else None
            result.append(item)
        return result

    def update_job_voice_snapshot(
        self,
        job_id: str,
        profile_snapshot: dict[str, Any],
        profile_snapshot_sha256: str,
    ) -> None:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET voice_profile_snapshot_json = ?,
                    voice_profile_snapshot_sha256 = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    json.dumps(profile_snapshot, ensure_ascii=False, sort_keys=True),
                    profile_snapshot_sha256,
                    job_id,
                ),
            )
        if cursor.rowcount != 1:
            raise KeyError(f"Generation job not found: {job_id}")

    def list_chunks(self, job_id: str) -> list[StoredChunk]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id AS database_id, logical_id AS id,
                       chapter_index, chunk_index, unit_ids_json,
                       status, attempts,
                       text_sha256, text_artifact_path,
                       audio_artifact_path, audio_sha256, duration_seconds
                FROM chunks WHERE job_id = ?
                ORDER BY chapter_index, chunk_index
                """,
                (job_id,),
            ).fetchall()
        return [self._stored_chunk(row) for row in rows]

    @staticmethod
    def _stored_chunk(row: object) -> StoredChunk:
        values = dict(row)
        values["unit_ids"] = tuple(json.loads(values.pop("unit_ids_json")))
        return StoredChunk(**values)

    def recover_interrupted(self, job_id: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE chunks SET status = 'pending' WHERE job_id = ? AND status = 'generating'",
                (job_id,),
            )
            connection.execute(
                "UPDATE jobs SET status = 'ready', updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND status IN ('generating', 'failed')",
                (job_id,),
            )

    def recover_interrupted_assemblies(self) -> int:
        """Make packaging jobs retryable after the serving process stopped."""
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET status = 'failed', error_message = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE status = 'assembling'",
                ("Audiobook assembly was interrupted. Build it again.",),
            )
        return cursor.rowcount

    def job_status(self, job_id: str) -> str:
        with self.database.connect() as connection:
            row = connection.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"Generation job not found: {job_id}")
        return str(row[0])

    def request_pause(self, job_id: str) -> None:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET status = 'pause_requested', updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status IN ('ready', 'generating')
                """,
                (job_id,),
            )
        if cursor.rowcount == 0 and self.job_status(job_id) not in {"paused", "completed"}:
            raise ValueError("Only ready or generating jobs can be paused")

    def mark_paused(self, job_id: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE jobs SET status = 'paused', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (job_id,),
            )

    def resume(self, job_id: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE jobs SET status = 'ready', updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND status IN ('paused', 'pause_requested')",
                (job_id,),
            )

    def start_job(self, job_id: str) -> None:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET status = 'generating', error_message = NULL,
                    compacted_at = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status IN ('ready', 'failed', 'completed')
                """,
                (job_id,),
            )
        if cursor.rowcount != 1:
            status = self.job_status(job_id)
            if status != "generating":
                raise ValueError(
                    "Only ready, failed, paused, or completed jobs can be started"
                )

    def begin_chunk(self, job_id: str, chunk_id: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE jobs SET status = 'generating', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (job_id,),
            )
            connection.execute(
                """
                UPDATE chunks SET status = 'generating', attempts = attempts + 1,
                                  updated_at = CURRENT_TIMESTAMP
                WHERE job_id = ? AND id = ?
                """,
                (job_id, chunk_id),
            )

    def begin_chunk_regeneration(self, job_id: str, chunk_id: str) -> None:
        with self.database.connect() as connection:
            job = connection.execute(
                "SELECT status FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job is None:
                raise KeyError(f"Generation job not found: {job_id}")
            if job["status"] in {"generating", "pause_requested", "assembling"}:
                raise ValueError("Wait for the active generation to stop before regenerating")
            chunk = connection.execute(
                "SELECT status FROM chunks WHERE job_id = ? AND id = ?",
                (job_id, chunk_id),
            ).fetchone()
            if chunk is None:
                raise KeyError(f"Chunk not found: {chunk_id}")
            if chunk["status"] != "completed":
                raise ValueError("Only a completed audio chunk can be regenerated")
            connection.execute(
                "UPDATE jobs SET status = 'generating', error_message = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (job_id,),
            )
            connection.execute(
                """
                UPDATE chunks SET status = 'generating', attempts = attempts + 1,
                                  updated_at = CURRENT_TIMESTAMP
                WHERE job_id = ? AND id = ?
                """,
                (job_id, chunk_id),
            )

    def complete_chunk(
        self, job_id: str, chunk_id: str, path: str, sha256: str, duration: float
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE chunks SET status = 'completed', audio_artifact_path = ?,
                    audio_sha256 = ?, duration_seconds = ?, updated_at = CURRENT_TIMESTAMP
                WHERE job_id = ? AND id = ?
                """,
                (path, sha256, duration, job_id, chunk_id),
            )

    def fail_chunk(self, job_id: str, chunk_id: str, message: str) -> None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT error_history_json FROM chunks WHERE job_id = ? AND id = ?",
                (job_id, chunk_id),
            ).fetchone()
            errors = json.loads(row[0]) if row else []
            errors.append({"message": message})
            connection.execute(
                """
                UPDATE chunks SET status = 'failed', error_history_json = ?,
                                  updated_at = CURRENT_TIMESTAMP
                WHERE job_id = ? AND id = ?
                """,
                (json.dumps(errors), job_id, chunk_id),
            )
            connection.execute(
                "UPDATE jobs SET status = 'failed', error_message = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (message, job_id),
            )

    def fail_chunk_regeneration(self, job_id: str, chunk_id: str, message: str) -> None:
        """Record a failed retry while keeping the previously verified audio usable."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT error_history_json FROM chunks WHERE job_id = ? AND id = ?",
                (job_id, chunk_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"Chunk not found: {chunk_id}")
            errors = json.loads(row[0])
            errors.append({"message": message})
            connection.execute(
                """
                UPDATE chunks SET status = 'completed', error_history_json = ?,
                                  updated_at = CURRENT_TIMESTAMP
                WHERE job_id = ? AND id = ?
                """,
                (json.dumps(errors), job_id, chunk_id),
            )
            connection.execute(
                "UPDATE jobs SET status = 'failed', error_message = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (message, job_id),
            )

    def reset_chunk(self, job_id: str, chunk_id: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE chunks SET status = 'pending', audio_artifact_path = NULL,
                    audio_sha256 = NULL, duration_seconds = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE job_id = ? AND id = ?
                """,
                (job_id, chunk_id),
            )
            connection.execute(
                """
                UPDATE jobs SET status = CASE
                    WHEN status = 'completed' THEN 'ready'
                    ELSE status END,
                    error_message = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (job_id,),
            )

    def complete_job(self, job_id: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE jobs SET status = 'completed', error_message = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (job_id,),
            )

    def begin_assembly(self, job_id: str) -> None:
        with self.database.connect() as connection:
            incomplete = connection.execute(
                """
                SELECT COUNT(*) FROM chunks
                WHERE job_id = ? AND (
                    status != 'completed'
                    OR audio_artifact_path IS NULL
                    OR audio_sha256 IS NULL
                )
                """,
                (job_id,),
            ).fetchone()[0]
            if incomplete:
                raise ValueError(
                    "Every editable audio master must be available before assembly"
                )
            cursor = connection.execute(
                "UPDATE jobs SET status = 'assembling', error_message = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ? "
                "AND status IN ('completed', 'failed')",
                (job_id,),
            )
        if cursor.rowcount != 1:
            status = self.job_status(job_id)
            if status != "assembling":
                raise ValueError("Only a completed generation job can be assembled")

    def fail_job(self, job_id: str, message: str) -> None:
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET status = 'failed', error_message = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (message, job_id),
            )
        if cursor.rowcount != 1:
            raise KeyError(f"Generation job not found: {job_id}")

    def compact_job_sources(self, job_id: str) -> list[str]:
        """Forget editable chunk masters after a verified M4B exists."""
        with self.database.connect() as connection:
            job = connection.execute(
                "SELECT status, compacted_at FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job is None:
                raise KeyError(f"Generation job not found: {job_id}")
            if job["status"] != "completed":
                raise ValueError("Only a completed audiobook can be finalized")
            if job["compacted_at"] is not None:
                raise ValueError("This audiobook has already been finalized")
            audiobook = connection.execute(
                "SELECT 1 FROM artifacts WHERE job_id = ? AND kind = 'audiobook'",
                (job_id,),
            ).fetchone()
            if audiobook is None:
                raise ValueError("Build and verify the M4B before freeing source storage")
            rows = connection.execute(
                "SELECT audio_artifact_path FROM chunks "
                "WHERE job_id = ? AND audio_artifact_path IS NOT NULL",
                (job_id,),
            ).fetchall()
            if not rows:
                raise ValueError("This job has no editable audio masters to remove")
            connection.execute(
                "UPDATE chunks SET audio_artifact_path = NULL, audio_sha256 = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE job_id = ?",
                (job_id,),
            )
            connection.execute(
                "UPDATE jobs SET compacted_at = CURRENT_TIMESTAMP, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (job_id,),
            )
        return [str(row["audio_artifact_path"]) for row in rows]

    def record_artifact(
        self,
        *,
        book_id: str,
        job_id: str,
        kind: str,
        relative_path: str,
        sha256: str,
        byte_size: int,
        metadata: dict[str, Any],
        chapter_index: int | None = None,
    ) -> str:
        artifact_id = uuid.uuid4().hex
        metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO artifacts(
                    id, book_id, job_id, kind, relative_path, sha256,
                    byte_size, metadata_json, chapter_index
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(book_id, relative_path) DO UPDATE SET
                    job_id = excluded.job_id,
                    kind = excluded.kind,
                    sha256 = excluded.sha256,
                    byte_size = excluded.byte_size,
                    metadata_json = excluded.metadata_json,
                    chapter_index = excluded.chapter_index,
                    created_at = CURRENT_TIMESTAMP
                """,
                (
                    artifact_id,
                    book_id,
                    job_id,
                    kind,
                    relative_path,
                    sha256,
                    byte_size,
                    metadata_json,
                    chapter_index,
                ),
            )
            row = connection.execute(
                "SELECT id FROM artifacts WHERE book_id = ? AND relative_path = ?",
                (book_id, relative_path),
            ).fetchone()
        return str(row["id"])

    def list_job_artifacts(self, job_id: str) -> list[StoredArtifact]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, kind, relative_path, sha256, byte_size,
                       chapter_index, metadata_json
                FROM artifacts WHERE job_id = ?
                ORDER BY CASE kind
                    WHEN 'chapter_audio' THEN 0
                    WHEN 'audiobook' THEN 1
                    WHEN 'narration_map' THEN 2
                    ELSE 3 END,
                    chapter_index, created_at
                """,
                (job_id,),
            ).fetchall()
        return [self._stored_artifact(row) for row in rows]

    def get_job_artifact(self, job_id: str, artifact_id: str) -> StoredArtifact:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT id, kind, relative_path, sha256, byte_size,
                       chapter_index, metadata_json
                FROM artifacts WHERE job_id = ? AND id = ?
                """,
                (job_id, artifact_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"Output artifact not found: {artifact_id}")
        return self._stored_artifact(row)

    def invalidate_job_outputs(self, job_id: str, chapter_index: int) -> list[str]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT relative_path FROM artifacts
                WHERE job_id = ? AND (
                    (kind = 'chapter_audio' AND chapter_index = ?)
                    OR kind IN ('audiobook', 'narration_map')
                )
                """,
                (job_id, chapter_index),
            ).fetchall()
            connection.execute(
                """
                DELETE FROM artifacts
                WHERE job_id = ? AND (
                    (kind = 'chapter_audio' AND chapter_index = ?)
                    OR kind IN ('audiobook', 'narration_map')
                )
                """,
                (job_id, chapter_index),
            )
        return [str(row["relative_path"]) for row in rows]

    @staticmethod
    def _stored_artifact(row: object) -> StoredArtifact:
        values = dict(row)
        values["metadata"] = json.loads(values.pop("metadata_json"))
        return StoredArtifact(**values)

    def finish_chunk_regeneration(self, job_id: str) -> None:
        with self.database.connect() as connection:
            job = connection.execute(
                "SELECT status FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job is None:
                raise KeyError(f"Generation job not found: {job_id}")
            remaining = connection.execute(
                "SELECT COUNT(*) FROM chunks WHERE job_id = ? AND status != 'completed'",
                (job_id,),
            ).fetchone()[0]
            if remaining == 0:
                status = "completed"
            elif job["status"] == "pause_requested":
                status = "paused"
            else:
                status = "ready"
            connection.execute(
                "UPDATE jobs SET status = ?, error_message = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, job_id),
            )

    def delete_generated_chunk(self, job_id: str, chunk_id: str) -> None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status FROM chunks WHERE job_id = ? AND id = ?",
                (job_id, chunk_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"Chunk not found: {chunk_id}")
            if row["status"] == "generating":
                raise ValueError("An actively generating chunk cannot be deleted")
            connection.execute(
                """
                UPDATE chunks SET status = 'pending', audio_artifact_path = NULL,
                    audio_sha256 = NULL, duration_seconds = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE job_id = ? AND id = ?
                """,
                (job_id, chunk_id),
            )
            connection.execute(
                """
                UPDATE jobs SET status = CASE
                    WHEN status IN ('completed', 'failed') THEN 'ready'
                    ELSE status END,
                    error_message = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (job_id,),
            )

    def delete_job(self, job_id: str) -> str:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT book_id, status FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Generation job not found: {job_id}")
            if row["status"] in {"generating", "pause_requested", "assembling"}:
                raise ValueError(
                    "Pause generation or wait for audiobook assembly before deleting it"
                )
            connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        return str(row["book_id"])

    def book_has_active_jobs(self, book_id: str) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM jobs
                WHERE book_id = ?
                  AND status IN ('generating', 'pause_requested', 'assembling')
                LIMIT 1
                """,
                (book_id,),
            ).fetchone()
        return row is not None
