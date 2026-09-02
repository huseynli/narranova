ALTER TABLE jobs ADD COLUMN provider_instance_id TEXT REFERENCES provider_instances(id);

UPDATE jobs
SET provider_instance_id = (
    SELECT provider_instance_id
    FROM narrator_profiles
    WHERE narrator_profiles.id = jobs.narrator_profile_id
)
WHERE narrator_profile_id IS NOT NULL;

CREATE INDEX idx_jobs_provider_instance
ON jobs(provider_instance_id);
