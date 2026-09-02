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
    book_id: str
    provider_name: str
    profile: dict[str, Any]


@dataclass(frozen=True)
class StoredJob:
    id: str
    book_id: str
    book_title: str
    status: str
    error_message: str | None
    created_at: str


@dataclass(frozen=True)
class ReusableChunk:
    audio_artifact_path: str
    audio_sha256: str
    duration_seconds: float


class GenerationRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def add_openmoss_provider(self, name: str, endpoint_url: str) -> str:
        provider_id = uuid.uuid4().hex
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO provider_instances(id, kind, name, endpoint_url)
                VALUES (?, 'openmoss', ?, ?)
                """,
                (provider_id, name, endpoint_url),
            )
        return provider_id

    def list_providers(self) -> list[StoredProvider]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, kind, name, endpoint_url, enabled
                FROM provider_instances ORDER BY created_at, name
                """
            ).fetchall()
        return [StoredProvider(**dict(row)) for row in rows]

    def list_voice_profiles(self, book_id: str) -> list[StoredVoiceProfile]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT v.id, v.book_id, p.name AS provider_name, v.profile_json
                FROM voice_profiles v
                JOIN provider_instances p ON p.id = v.provider_instance_id
                WHERE v.book_id = ? ORDER BY v.created_at, v.id
                """,
                (book_id,),
            ).fetchall()
        return [
            StoredVoiceProfile(
                id=row["id"],
                book_id=row["book_id"],
                provider_name=row["provider_name"],
                profile=json.loads(row["profile_json"]),
            )
            for row in rows
        ]

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
                SELECT id AS database_id, logical_id AS id, status, attempts,
                       text_sha256, text_artifact_path, audio_artifact_path,
                       audio_sha256, duration_seconds
                FROM chunks WHERE job_id = ? AND id = ?
                """,
                (job_id, chunk_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"Chunk not found: {chunk_id}")
        return StoredChunk(**dict(row))

    def find_reusable_chunk(
        self,
        *,
        book_id: str,
        excluding_job_id: str,
        logical_id: str,
        text_sha256: str,
    ) -> ReusableChunk | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT c.audio_artifact_path, c.audio_sha256, c.duration_seconds
                FROM chunks c
                JOIN jobs j ON j.id = c.job_id
                WHERE j.book_id = ? AND j.id != ? AND c.logical_id = ?
                  AND c.text_sha256 = ? AND c.status = 'completed'
                  AND c.audio_artifact_path IS NOT NULL
                  AND c.audio_sha256 IS NOT NULL
                  AND c.duration_seconds IS NOT NULL
                ORDER BY c.updated_at DESC LIMIT 1
                """,
                (book_id, excluding_job_id, logical_id, text_sha256),
            ).fetchone()
        return ReusableChunk(**dict(row)) if row else None

    def add_voice_profile(
        self,
        *,
        profile_id: str,
        book_id: str,
        provider_id: str,
        profile: dict[str, Any],
        profile_sha256: str,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO voice_profiles(
                    id, book_id, provider_instance_id, profile_json, profile_sha256
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    profile_id,
                    book_id,
                    provider_id,
                    json.dumps(profile, ensure_ascii=False, sort_keys=True),
                    profile_sha256,
                ),
            )

    def get_voice_and_provider(self, profile_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT v.id, v.book_id, v.profile_json, v.profile_sha256,
                       p.id AS provider_id, p.kind AS provider_kind,
                       p.name AS provider_name, p.endpoint_url, p.configuration_json
                FROM voice_profiles v
                JOIN provider_instances p ON p.id = v.provider_instance_id
                WHERE v.id = ? AND p.enabled = 1
                """,
                (profile_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Voice profile not found: {profile_id}")
        result = dict(row)
        result["profile"] = json.loads(result.pop("profile_json"))
        result["provider_configuration"] = json.loads(result.pop("configuration_json"))
        return result

    def create_job(
        self,
        *,
        job_id: str,
        book_id: str,
        plan_id: str,
        voice_profile_id: str,
        chunks: list[tuple[SynthesisChunk, str, str]],
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs(id, book_id, narration_plan_id, voice_profile_id, status)
                VALUES (?, ?, ?, ?, 'ready')
                """,
                (job_id, book_id, plan_id, voice_profile_id),
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
                       v.profile_json, v.profile_sha256,
                       p.kind AS provider_kind, p.endpoint_url, p.configuration_json
                FROM jobs j
                JOIN books b ON b.id = j.book_id
                JOIN voice_profiles v ON v.id = j.voice_profile_id
                JOIN provider_instances p ON p.id = v.provider_instance_id
                WHERE j.id = ?
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Generation job not found: {job_id}")
        result = dict(row)
        result["profile"] = json.loads(result.pop("profile_json"))
        result["provider_configuration"] = json.loads(result.pop("configuration_json"))
        return result

    def list_chunks(self, job_id: str) -> list[StoredChunk]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id AS database_id, logical_id AS id, status, attempts,
                       text_sha256, text_artifact_path,
                       audio_artifact_path, audio_sha256, duration_seconds
                FROM chunks WHERE job_id = ?
                ORDER BY chapter_index, chunk_index
                """,
                (job_id,),
            ).fetchall()
        return [StoredChunk(**dict(row)) for row in rows]

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

    def complete_job(self, job_id: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE jobs SET status = 'completed', error_message = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (job_id,),
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
            if row["status"] in {"generating", "pause_requested"}:
                raise ValueError("Pause the generation job before deleting it")
            connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        return str(row["book_id"])

    def book_has_active_jobs(self, book_id: str) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM jobs
                WHERE book_id = ? AND status IN ('generating', 'pause_requested')
                LIMIT 1
                """,
                (book_id,),
            ).fetchone()
        return row is not None
