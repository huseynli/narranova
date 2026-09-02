"""Durable generation state definitions."""

from enum import Enum


class JobStatus(str, Enum):
    UPLOADED = "uploaded"
    PLANNED = "planned"
    CHOOSING_VOICE = "choosing_voice"
    READY = "ready"
    GENERATING = "generating"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    ASSEMBLING = "assembling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ChunkStatus(str, Enum):
    PENDING = "pending"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"
