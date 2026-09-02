CREATE TABLE narrator_profiles (
    id TEXT PRIMARY KEY,
    provider_instance_id TEXT NOT NULL REFERENCES provider_instances(id),
    profile_json TEXT NOT NULL,
    profile_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO narrator_profiles(
    id, provider_instance_id, profile_json, profile_sha256, created_at
)
SELECT id, provider_instance_id, profile_json, profile_sha256, created_at
FROM voice_profiles;

ALTER TABLE jobs ADD COLUMN narrator_profile_id TEXT REFERENCES narrator_profiles(id);
ALTER TABLE jobs ADD COLUMN voice_profile_snapshot_json TEXT;
ALTER TABLE jobs ADD COLUMN voice_profile_snapshot_sha256 TEXT;

UPDATE jobs
SET narrator_profile_id = voice_profile_id
WHERE voice_profile_id IS NOT NULL;

UPDATE jobs
SET voice_profile_snapshot_json = (
        SELECT profile_json FROM narrator_profiles
        WHERE narrator_profiles.id = jobs.narrator_profile_id
    ),
    voice_profile_snapshot_sha256 = (
        SELECT profile_sha256 FROM narrator_profiles
        WHERE narrator_profiles.id = jobs.narrator_profile_id
    )
WHERE narrator_profile_id IS NOT NULL;

UPDATE jobs
SET voice_profile_id = NULL
WHERE narrator_profile_id IS NOT NULL;

DELETE FROM voice_profiles;

CREATE INDEX idx_narrator_profiles_provider
ON narrator_profiles(provider_instance_id);
