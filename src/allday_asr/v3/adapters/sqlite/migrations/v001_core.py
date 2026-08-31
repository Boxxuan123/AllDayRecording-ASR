"""V3 Core schema version 1. This database is independent from every V2 table."""

SQL = """
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE devices (
    device_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('watch', 'phone', 'computer')),
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'revoked')),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    last_seen_at TEXT,
    tombstoned_at TEXT,
    legacy_ref TEXT UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE recording_sessions (
    session_id TEXT PRIMARY KEY,
    captured_start TEXT NOT NULL,
    captured_end TEXT,
    timezone TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'capturing', 'closing', 'recovering', 'quarantined', 'sealed',
        'phone_verified', 'computer_ingested', 'admission_pending',
        'admission_blocked', 'ready_for_processing'
    )),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    status_code TEXT NOT NULL CHECK(status_code IN (
        'awaiting_upload', 'verifying', 'backup_required', 'ready',
        'processing', 'needs_review', 'available', 'failed', 'stale'
    )),
    current_stage TEXT,
    progress REAL NOT NULL CHECK(progress >= 0.0 AND progress <= 1.0),
    blocking_reason TEXT,
    tombstoned_at TEXT,
    legacy_ref TEXT UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(captured_end IS NULL OR captured_end >= captured_start)
);

CREATE INDEX idx_recording_sessions_capture
ON recording_sessions(captured_start, session_id);

CREATE INDEX idx_recording_sessions_status
ON recording_sessions(status_code, updated_at, session_id);

CREATE TABLE audio_assets (
    asset_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE CHECK(length(sha256) = 64),
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    duration_ms INTEGER NOT NULL CHECK(duration_ms >= 0),
    format TEXT NOT NULL CHECK(format IN ('wav', 'm4a', 'flac')),
    media_id TEXT NOT NULL UNIQUE,
    legacy_ref TEXT UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE audio_replicas (
    replica_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    storage_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'discovered', 'receiving', 'stored', 'verified', 'available',
        'failed_retryable', 'quarantined', 'delete_pending', 'deleted'
    )),
    verified_at TEXT,
    legacy_ref TEXT UNIQUE,
    created_at TEXT NOT NULL,
    UNIQUE(replica_id, asset_id),
    FOREIGN KEY(asset_id) REFERENCES audio_assets(asset_id) ON DELETE RESTRICT,
    FOREIGN KEY(device_id) REFERENCES devices(device_id) ON DELETE RESTRICT
);

CREATE INDEX idx_audio_replicas_asset
ON audio_replicas(asset_id, state, replica_id);

CREATE TABLE capture_segments (
    segment_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    replica_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence >= 0),
    session_start_ms INTEGER NOT NULL CHECK(session_start_ms >= 0),
    session_end_ms INTEGER NOT NULL,
    source_start_ms INTEGER NOT NULL CHECK(source_start_ms >= 0),
    source_end_ms INTEGER NOT NULL,
    start_sample INTEGER,
    captured_at TEXT NOT NULL,
    legacy_ref TEXT UNIQUE,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, sequence),
    CHECK(session_end_ms > session_start_ms),
    CHECK(source_end_ms > source_start_ms),
    CHECK(start_sample IS NULL OR start_sample >= 0),
    FOREIGN KEY(session_id) REFERENCES recording_sessions(session_id) ON DELETE RESTRICT,
    FOREIGN KEY(asset_id) REFERENCES audio_assets(asset_id) ON DELETE RESTRICT,
    FOREIGN KEY(replica_id, asset_id)
        REFERENCES audio_replicas(replica_id, asset_id) ON DELETE RESTRICT
);

CREATE INDEX idx_capture_segments_session_time
ON capture_segments(session_id, session_start_ms, session_end_ms);

CREATE TABLE session_manifests (
    manifest_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL UNIQUE,
    schema_version TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE CHECK(length(sha256) = 64),
    storage_ref TEXT NOT NULL,
    entries_json TEXT NOT NULL,
    legacy_ref TEXT UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES recording_sessions(session_id) ON DELETE RESTRICT
);

CREATE TABLE processing_runs (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    pipeline_version TEXT NOT NULL,
    input_revision INTEGER NOT NULL CHECK(input_revision >= 1),
    status TEXT NOT NULL CHECK(status IN (
        'created', 'queued', 'running', 'waiting_review', 'succeeded',
        'failed_retryable', 'failed_final', 'cancel_requested', 'cancelled', 'stale'
    )),
    config_digest TEXT NOT NULL,
    current_stage TEXT,
    progress REAL NOT NULL CHECK(progress >= 0.0 AND progress <= 1.0),
    completed_at TEXT,
    error TEXT,
    legacy_ref TEXT UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES recording_sessions(session_id) ON DELETE RESTRICT
);

CREATE INDEX idx_processing_runs_session
ON processing_runs(session_id, created_at, run_id);

CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT,
    kind TEXT NOT NULL,
    producer TEXT NOT NULL,
    producer_version TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    input_refs_json TEXT NOT NULL,
    storage_ref TEXT NOT NULL,
    sha256 TEXT CHECK(sha256 IS NULL OR length(sha256) = 64),
    size_bytes INTEGER CHECK(size_bytes IS NULL OR size_bytes >= 0),
    status TEXT NOT NULL CHECK(status IN ('active', 'stale', 'invalid', 'quarantined')),
    metadata_json TEXT NOT NULL,
    legacy_ref TEXT UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES processing_runs(run_id) ON DELETE RESTRICT
);

CREATE INDEX idx_artifacts_run_kind
ON artifacts(run_id, kind, created_at, artifact_id);

CREATE TABLE correction_operations (
    correction_id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    before_revision INTEGER CHECK(before_revision IS NULL OR before_revision >= 1),
    patch_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    legacy_ref TEXT UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_corrections_target
ON correction_operations(target_type, target_id, created_at, correction_id);

CREATE TABLE change_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    operation TEXT NOT NULL CHECK(operation IN ('upsert', 'tombstone')),
    payload_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_change_events_resource
ON change_events(resource_type, resource_id, sequence);

CREATE TABLE audit_entries (
    audit_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    details_json TEXT NOT NULL,
    legacy_ref TEXT UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_audit_entries_target
ON audit_entries(target_type, target_id, created_at, audit_id);

CREATE TABLE idempotency_records (
    idempotency_key TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('started', 'completed', 'failed')),
    response_json TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE tombstones (
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    reason TEXT,
    deleted_at TEXT NOT NULL,
    PRIMARY KEY(resource_type, resource_id)
);

CREATE TABLE legacy_import_runs (
    import_id TEXT PRIMARY KEY,
    source_namespace TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_database_sha256 TEXT NOT NULL,
    source_schema_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed')),
    report_json TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX idx_legacy_import_runs_source
ON legacy_import_runs(source_namespace, started_at, import_id);

CREATE TRIGGER protect_audio_assets_from_update
BEFORE UPDATE ON audio_assets BEGIN
    SELECT RAISE(ABORT, 'audio asset is immutable');
END;

CREATE TRIGGER protect_audio_assets_from_delete
BEFORE DELETE ON audio_assets BEGIN
    SELECT RAISE(ABORT, 'audio asset cannot be deleted directly');
END;

CREATE TRIGGER protect_capture_segments_from_update
BEFORE UPDATE ON capture_segments BEGIN
    SELECT RAISE(ABORT, 'capture segment is immutable');
END;

CREATE TRIGGER protect_capture_segments_from_delete
BEFORE DELETE ON capture_segments BEGIN
    SELECT RAISE(ABORT, 'capture segment cannot be deleted directly');
END;

CREATE TRIGGER protect_artifacts_from_update
BEFORE UPDATE ON artifacts BEGIN
    SELECT RAISE(ABORT, 'artifact is immutable');
END;

CREATE TRIGGER protect_artifacts_from_delete
BEFORE DELETE ON artifacts BEGIN
    SELECT RAISE(ABORT, 'artifact cannot be deleted directly');
END;

CREATE TRIGGER protect_corrections_from_update
BEFORE UPDATE ON correction_operations BEGIN
    SELECT RAISE(ABORT, 'correction operation is immutable');
END;

CREATE TRIGGER protect_corrections_from_delete
BEFORE DELETE ON correction_operations BEGIN
    SELECT RAISE(ABORT, 'correction operation cannot be deleted directly');
END;

CREATE TRIGGER protect_session_manifests_from_update
BEFORE UPDATE ON session_manifests BEGIN
    SELECT RAISE(ABORT, 'session manifest is immutable');
END;

CREATE TRIGGER protect_session_manifests_from_delete
BEFORE DELETE ON session_manifests BEGIN
    SELECT RAISE(ABORT, 'session manifest cannot be deleted directly');
END;

CREATE TRIGGER protect_change_events_from_update
BEFORE UPDATE ON change_events BEGIN
    SELECT RAISE(ABORT, 'change event is immutable');
END;

CREATE TRIGGER protect_change_events_from_delete
BEFORE DELETE ON change_events BEGIN
    SELECT RAISE(ABORT, 'change event cannot be deleted directly');
END;

CREATE TRIGGER protect_audit_entries_from_update
BEFORE UPDATE ON audit_entries BEGIN
    SELECT RAISE(ABORT, 'audit entry is immutable');
END;

CREATE TRIGGER protect_audit_entries_from_delete
BEFORE DELETE ON audit_entries BEGIN
    SELECT RAISE(ABORT, 'audit entry cannot be deleted directly');
END;

CREATE TRIGGER protect_tombstones_from_update
BEFORE UPDATE ON tombstones BEGIN
    SELECT RAISE(ABORT, 'tombstone is immutable');
END;

CREATE TRIGGER protect_tombstones_from_delete
BEFORE DELETE ON tombstones BEGIN
    SELECT RAISE(ABORT, 'tombstone cannot be deleted directly');
END;
"""
