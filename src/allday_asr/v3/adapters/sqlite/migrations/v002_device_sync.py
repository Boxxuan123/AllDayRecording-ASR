"""V3 Device Trust and Mobile Sync persistence."""

SQL = """
CREATE TABLE device_credentials (
    credential_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    key_id TEXT NOT NULL UNIQUE,
    algorithm TEXT NOT NULL,
    public_key TEXT NOT NULL,
    scopes_json TEXT NOT NULL,
    passkey_credential_ref TEXT NOT NULL,
    revoked_at TEXT,
    last_used_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(device_id) REFERENCES devices(device_id) ON DELETE RESTRICT
);

CREATE INDEX idx_device_credentials_device
ON device_credentials(device_id, revoked_at, credential_id);

CREATE TABLE pairing_records (
    pairing_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    receiver_id TEXT NOT NULL,
    passkey_credential_ref TEXT NOT NULL,
    paired_at TEXT NOT NULL,
    revoked_at TEXT,
    UNIQUE(device_id, receiver_id),
    FOREIGN KEY(device_id) REFERENCES devices(device_id) ON DELETE RESTRICT
);

CREATE TABLE sync_cursors (
    device_id TEXT PRIMARY KEY,
    projection_version INTEGER NOT NULL CHECK(projection_version = 1),
    acknowledged_sequence INTEGER NOT NULL CHECK(acknowledged_sequence >= 0),
    updated_at TEXT NOT NULL,
    FOREIGN KEY(device_id) REFERENCES devices(device_id) ON DELETE RESTRICT
);

CREATE TABLE client_operations (
    operation_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    base_revision INTEGER CHECK(base_revision IS NULL OR base_revision >= 1),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'pending', 'applied', 'conflict', 'rejected'
    )),
    receipt_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    FOREIGN KEY(device_id) REFERENCES devices(device_id) ON DELETE RESTRICT
);

CREATE INDEX idx_client_operations_device
ON client_operations(device_id, created_at, operation_id);

CREATE TRIGGER protect_client_operations_from_update
BEFORE UPDATE ON client_operations BEGIN
    SELECT RAISE(ABORT, 'client operation is immutable');
END;

CREATE TRIGGER protect_client_operations_from_delete
BEFORE DELETE ON client_operations BEGIN
    SELECT RAISE(ABORT, 'client operation cannot be deleted directly');
END;

CREATE TRIGGER protect_pairing_records_from_delete
BEFORE DELETE ON pairing_records BEGIN
    SELECT RAISE(ABORT, 'pairing record cannot be deleted directly');
END;

DROP TRIGGER protect_change_events_from_update;

UPDATE change_events
SET payload_json = (
    SELECT json_object(
        'session_id', session_id,
        'captured_start', captured_start,
        'captured_end', captured_end,
        'timezone', timezone,
        'state', state,
        'revision', revision,
        'status_code', status_code,
        'current_stage', current_stage,
        'progress', progress,
        'blocking_reason', blocking_reason
    )
    FROM recording_sessions
    WHERE recording_sessions.session_id = change_events.resource_id
)
WHERE resource_type = 'recording_session'
  AND operation = 'upsert'
  AND payload_json IS NULL;

UPDATE change_events
SET payload_json = (
    SELECT json_object(
        'asset_id', asset_id,
        'sha256', sha256,
        'size_bytes', size_bytes,
        'duration_ms', duration_ms,
        'format', format,
        'media_id', media_id
    )
    FROM audio_assets
    WHERE audio_assets.asset_id = change_events.resource_id
)
WHERE resource_type = 'audio_asset'
  AND operation = 'upsert'
  AND payload_json IS NULL;

UPDATE change_events
SET payload_json = (
    SELECT json_object(
        'run_id', run_id,
        'session_id', session_id,
        'pipeline_version', pipeline_version,
        'input_revision', input_revision,
        'status', status,
        'current_stage', current_stage,
        'progress', progress,
        'completed_at', completed_at,
        'error', error
    )
    FROM processing_runs
    WHERE processing_runs.run_id = change_events.resource_id
)
WHERE resource_type = 'processing_run'
  AND operation = 'upsert'
  AND payload_json IS NULL;

CREATE TRIGGER protect_change_events_from_update
BEFORE UPDATE ON change_events BEGIN
    SELECT RAISE(ABORT, 'change event is immutable');
END;
"""
