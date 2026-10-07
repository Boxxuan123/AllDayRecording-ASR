"""PC-private orchestration metadata; no phone contract or domain changes."""
SQL = """
CREATE TABLE daily_generation_jobs (
    job_id TEXT PRIMARY KEY,
    local_date TEXT NOT NULL,
    timezone TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    generation_version TEXT NOT NULL,
    status TEXT NOT NULL,
    origin TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    attempt_limit INTEGER NOT NULL DEFAULT 2 CHECK(attempt_limit >= 2),
    automatic_retry_blocked INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    heartbeat_at TEXT,
    available_at TEXT NOT NULL,
    summary_id TEXT,
    summary_revision INTEGER,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(local_date, timezone, source_fingerprint, generation_version)
);
CREATE INDEX idx_daily_generation_claim
ON daily_generation_jobs(status,available_at,local_date DESC);
"""
