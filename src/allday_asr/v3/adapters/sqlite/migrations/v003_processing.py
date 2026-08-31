"""V3 durable processing, admission, and evidence projection schema."""

SQL = """
ALTER TABLE processing_runs
ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1);

DROP TRIGGER protect_change_events_from_update;

UPDATE change_events
SET payload_json = json_set(
    COALESCE(payload_json, '{}'),
    '$.revision',
    COALESCE((
        SELECT revision FROM processing_runs
        WHERE processing_runs.run_id = change_events.resource_id
    ), 1)
)
WHERE resource_type = 'processing_run' AND operation = 'upsert';

CREATE TRIGGER protect_change_events_from_update
BEFORE UPDATE ON change_events BEGIN
    SELECT RAISE(ABORT, 'change event is immutable');
END;

CREATE UNIQUE INDEX idx_processing_runs_one_effective
ON processing_runs(session_id, input_revision, pipeline_version)
WHERE status IN (
    'created', 'queued', 'running', 'waiting_review',
    'failed_retryable', 'cancel_requested', 'stale'
);

CREATE TABLE processing_jobs (
    job_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'created', 'queued', 'running', 'waiting_review', 'succeeded',
        'failed_retryable', 'failed_final', 'cancel_requested', 'cancelled', 'stale'
    )),
    priority INTEGER NOT NULL DEFAULT 0,
    request_json TEXT NOT NULL,
    result_json TEXT,
    error TEXT,
    available_at TEXT NOT NULL,
    lease_owner TEXT,
    heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY(run_id) REFERENCES processing_runs(run_id) ON DELETE RESTRICT
);

CREATE INDEX idx_processing_jobs_claim
ON processing_jobs(status, available_at, priority DESC, created_at, job_id);

CREATE TABLE stage_runs (
    stage_run_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    optional INTEGER NOT NULL CHECK(optional IN (0, 1)),
    status TEXT NOT NULL CHECK(status IN (
        'pending', 'running', 'waiting_review', 'succeeded',
        'failed', 'cancelled', 'stale'
    )),
    progress REAL NOT NULL DEFAULT 0.0 CHECK(progress >= 0.0 AND progress <= 1.0),
    output_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(run_id, stage),
    UNIQUE(run_id, ordinal),
    FOREIGN KEY(run_id) REFERENCES processing_runs(run_id) ON DELETE RESTRICT
);

CREATE INDEX idx_stage_runs_ready
ON stage_runs(run_id, status, ordinal);

CREATE TABLE stage_attempts (
    attempt_id TEXT PRIMARY KEY,
    stage_run_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL CHECK(attempt_number >= 1),
    status TEXT NOT NULL CHECK(status IN (
        'running', 'succeeded', 'failed', 'cancelled', 'lost_lease'
    )),
    worker_id TEXT NOT NULL,
    config_json TEXT NOT NULL,
    checkpoint_json TEXT,
    log_summary TEXT,
    error TEXT,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(stage_run_id, attempt_number),
    FOREIGN KEY(stage_run_id) REFERENCES stage_runs(stage_run_id) ON DELETE RESTRICT
);

CREATE INDEX idx_stage_attempts_stage
ON stage_attempts(stage_run_id, attempt_number DESC);

CREATE TABLE worker_leases (
    lease_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL UNIQUE,
    worker_id TEXT NOT NULL,
    lease_token TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('active', 'released', 'expired', 'lost')),
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    released_at TEXT,
    FOREIGN KEY(job_id) REFERENCES processing_jobs(job_id) ON DELETE RESTRICT,
    FOREIGN KEY(attempt_id) REFERENCES stage_attempts(attempt_id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX idx_worker_leases_one_active_job
ON worker_leases(job_id) WHERE status = 'active';

CREATE INDEX idx_worker_leases_expiry
ON worker_leases(status, expires_at);

CREATE TABLE backup_evidence (
    evidence_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    storage_kind TEXT NOT NULL CHECK(storage_kind IN ('independent_device', 'network')),
    digest TEXT NOT NULL CHECK(length(digest) = 64),
    status TEXT NOT NULL CHECK(status IN ('verified', 'failed')),
    restore_checked_at TEXT,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES recording_sessions(session_id) ON DELETE RESTRICT
);

CREATE INDEX idx_backup_evidence_session
ON backup_evidence(session_id, status, restore_checked_at, created_at);

CREATE TABLE speaker_tracks (
    speaker_track_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    label TEXT NOT NULL,
    source_artifact_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, label),
    FOREIGN KEY(session_id) REFERENCES recording_sessions(session_id) ON DELETE RESTRICT,
    FOREIGN KEY(run_id) REFERENCES processing_runs(run_id) ON DELETE RESTRICT,
    FOREIGN KEY(source_artifact_id) REFERENCES artifacts(artifact_id) ON DELETE RESTRICT
);

CREATE TABLE utterances (
    utterance_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    source_artifact_id TEXT NOT NULL,
    speaker_track_id TEXT,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
    end_ms INTEGER NOT NULL CHECK(end_ms > start_ms),
    text TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    status TEXT NOT NULL CHECK(status IN ('active', 'stale')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, ordinal),
    FOREIGN KEY(session_id) REFERENCES recording_sessions(session_id) ON DELETE RESTRICT,
    FOREIGN KEY(run_id) REFERENCES processing_runs(run_id) ON DELETE RESTRICT,
    FOREIGN KEY(source_artifact_id) REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    FOREIGN KEY(speaker_track_id) REFERENCES speaker_tracks(speaker_track_id) ON DELETE RESTRICT
);

CREATE INDEX idx_utterances_session_time
ON utterances(session_id, start_ms, end_ms, utterance_id);

CREATE TABLE artifact_dependencies (
    artifact_id TEXT NOT NULL,
    input_type TEXT NOT NULL,
    input_id TEXT NOT NULL,
    input_revision INTEGER NOT NULL CHECK(input_revision >= 1),
    PRIMARY KEY(artifact_id, input_type, input_id, input_revision),
    FOREIGN KEY(artifact_id) REFERENCES artifacts(artifact_id) ON DELETE RESTRICT
);

CREATE INDEX idx_artifact_dependencies_input
ON artifact_dependencies(input_type, input_id, input_revision, artifact_id);

CREATE TABLE artifact_status_events (
    status_event_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('stale', 'invalid', 'quarantined')),
    reason TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL CHECK(source_revision >= 1),
    created_at TEXT NOT NULL,
    UNIQUE(artifact_id, status, source_type, source_id, source_revision),
    FOREIGN KEY(artifact_id) REFERENCES artifacts(artifact_id) ON DELETE RESTRICT
);

CREATE INDEX idx_artifact_status_events_artifact
ON artifact_status_events(artifact_id, created_at, status_event_id);

CREATE TRIGGER protect_backup_evidence_from_update
BEFORE UPDATE ON backup_evidence BEGIN
    SELECT RAISE(ABORT, 'backup evidence is immutable');
END;

CREATE TRIGGER protect_backup_evidence_from_delete
BEFORE DELETE ON backup_evidence BEGIN
    SELECT RAISE(ABORT, 'backup evidence cannot be deleted directly');
END;

CREATE TRIGGER protect_artifact_dependencies_from_update
BEFORE UPDATE ON artifact_dependencies BEGIN
    SELECT RAISE(ABORT, 'artifact dependency is immutable');
END;

CREATE TRIGGER protect_artifact_dependencies_from_delete
BEFORE DELETE ON artifact_dependencies BEGIN
    SELECT RAISE(ABORT, 'artifact dependency cannot be deleted directly');
END;

CREATE TRIGGER protect_artifact_status_events_from_update
BEFORE UPDATE ON artifact_status_events BEGIN
    SELECT RAISE(ABORT, 'artifact status event is immutable');
END;

CREATE TRIGGER protect_artifact_status_events_from_delete
BEFORE DELETE ON artifact_status_events BEGIN
    SELECT RAISE(ABORT, 'artifact status event cannot be deleted directly');
END;
"""
