-- Version 7 was used by an unreleased development build. Keep this migration at
-- 8 so those local databases can upgrade safely without reusing that schema.

ALTER TABLE jobs ADD COLUMN connection_configuration_snapshot_json TEXT;

UPDATE jobs
SET connection_configuration_snapshot_json = (
    SELECT configuration_json
    FROM provider_instances
    WHERE provider_instances.id = jobs.provider_instance_id
)
WHERE connection_configuration_snapshot_json IS NULL;

CREATE TABLE connection_benchmark_runs (
    id TEXT PRIMARY KEY,
    provider_instance_id TEXT NOT NULL
        REFERENCES provider_instances(id) ON DELETE CASCADE,
    mode TEXT NOT NULL CHECK (mode IN ('single', 'auto')),
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    requested_frames_json TEXT NOT NULL,
    active_stream_chunk_frames INTEGER,
    benchmark_text_sha256 TEXT NOT NULL,
    voice_pair_id TEXT NOT NULL,
    seed INTEGER NOT NULL CHECK (seed >= 0),
    max_new_tokens INTEGER NOT NULL CHECK (max_new_tokens > 0),
    results_json TEXT NOT NULL DEFAULT '[]',
    recommended_stream_chunk_frames INTEGER,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
);

CREATE INDEX idx_connection_benchmark_runs_provider_created_v2
ON connection_benchmark_runs(provider_instance_id, created_at DESC);

CREATE UNIQUE INDEX idx_connection_benchmark_runs_one_running_v2
ON connection_benchmark_runs(provider_instance_id)
WHERE status = 'running';
