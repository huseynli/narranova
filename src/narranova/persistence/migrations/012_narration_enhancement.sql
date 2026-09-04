CREATE TABLE book_narration_enhancement (
    book_id TEXT PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
    settings_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE jobs ADD COLUMN narration_enhancement_snapshot_json TEXT;
ALTER TABLE chunks ADD COLUMN synthesis_text_artifact_path TEXT;
ALTER TABLE chunks ADD COLUMN synthesis_text_sha256 TEXT;
