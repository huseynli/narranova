ALTER TABLE jobs ADD COLUMN lease_owner TEXT;
ALTER TABLE jobs ADD COLUMN lease_expires_at REAL;

CREATE TABLE provider_work_leases (
    provider_instance_id TEXT PRIMARY KEY
        REFERENCES provider_instances(id) ON DELETE CASCADE,
    owner_id TEXT NOT NULL,
    lease_expires_at REAL NOT NULL
);

CREATE INDEX idx_jobs_lease_expires_at ON jobs(lease_expires_at);
