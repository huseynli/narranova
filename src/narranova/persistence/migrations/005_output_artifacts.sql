ALTER TABLE artifacts ADD COLUMN chapter_index INTEGER;

CREATE INDEX idx_artifacts_job_kind
ON artifacts(job_id, kind, chapter_index);
