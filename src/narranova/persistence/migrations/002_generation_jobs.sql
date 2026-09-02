ALTER TABLE chunks ADD COLUMN text_artifact_path TEXT;
ALTER TABLE chunks ADD COLUMN unit_ids_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE chunks ADD COLUMN logical_id TEXT;
