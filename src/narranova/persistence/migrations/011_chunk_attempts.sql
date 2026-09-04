CREATE TABLE chunk_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    status TEXT NOT NULL CHECK (status IN ('completed', 'failed', 'cancelled')),
    provider_request_id TEXT,
    wall_seconds REAL,
    audio_duration_seconds REAL,
    realtime_factor REAL,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (chunk_id, attempt_number)
);

CREATE INDEX idx_chunk_attempts_chunk ON chunk_attempts(chunk_id, attempt_number);
