"""V3.1-B conservative self-identity projection and correction baseline."""

SQL = """
ALTER TABLE utterances RENAME TO utterances_v31a;

CREATE TABLE utterances (
    utterance_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    source_artifact_id TEXT NOT NULL,
    speaker_track_id TEXT,
    original_speaker_track_id TEXT,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
    end_ms INTEGER NOT NULL CHECK(end_ms > start_ms),
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    text TEXT NOT NULL,
    original_text TEXT NOT NULL,
    identity TEXT NOT NULL CHECK(identity IN ('self', 'not_self', 'unknown')),
    original_identity TEXT NOT NULL CHECK(original_identity IN ('self', 'not_self', 'unknown')),
    identity_evidence_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    status TEXT NOT NULL CHECK(status IN ('active', 'stale')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, ordinal),
    FOREIGN KEY(session_id) REFERENCES recording_sessions(session_id) ON DELETE RESTRICT,
    FOREIGN KEY(run_id) REFERENCES processing_runs(run_id) ON DELETE RESTRICT,
    FOREIGN KEY(source_artifact_id) REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    FOREIGN KEY(speaker_track_id) REFERENCES speaker_tracks(speaker_track_id) ON DELETE RESTRICT,
    FOREIGN KEY(original_speaker_track_id) REFERENCES speaker_tracks(speaker_track_id) ON DELETE RESTRICT
);

INSERT INTO utterances (
    utterance_id, session_id, run_id, source_artifact_id,
    speaker_track_id, original_speaker_track_id, ordinal,
    start_ms, end_ms, start_at, end_at, text, original_text,
    identity, original_identity, identity_evidence_json, evidence_json,
    revision, status, created_at, updated_at
)
SELECT
    utterance_id, session_id, run_id, source_artifact_id,
    speaker_track_id, original_speaker_track_id, ordinal,
    start_ms, end_ms, start_at, end_at, text, original_text,
    'unknown', 'unknown',
    '{"source":"none","decision":"unknown","reason":"predates_v31b_identity"}',
    evidence_json, revision, status, created_at, updated_at
FROM utterances_v31a;

DROP TABLE utterances_v31a;

CREATE INDEX idx_utterances_session_time
ON utterances(session_id, start_ms, end_ms, utterance_id);

CREATE INDEX idx_utterances_session_identity
ON utterances(session_id, identity, start_ms, utterance_id);

ALTER TABLE sync_cursors RENAME TO sync_cursors_v31a;

CREATE TABLE sync_cursors (
    device_id TEXT PRIMARY KEY,
    projection_version INTEGER NOT NULL CHECK(projection_version = 3),
    acknowledged_sequence INTEGER NOT NULL CHECK(acknowledged_sequence >= 0),
    updated_at TEXT NOT NULL,
    FOREIGN KEY(device_id) REFERENCES devices(device_id) ON DELETE RESTRICT
);

INSERT INTO sync_cursors (
    device_id, projection_version, acknowledged_sequence, updated_at
)
SELECT device_id, 3, 0, updated_at FROM sync_cursors_v31a;

DROP TABLE sync_cursors_v31a;

DROP TRIGGER protect_change_events_from_update;

UPDATE change_events
SET payload_json = json_set(
    payload_json,
    '$.identity', 'unknown',
    '$.original_identity', 'unknown',
    '$.identity_evidence', json(
        '{"source":"none","decision":"unknown","reason":"predates_v31b_identity"}'
    )
)
WHERE resource_type = 'utterance' AND operation = 'upsert';

CREATE TRIGGER protect_change_events_from_update
BEFORE UPDATE ON change_events BEGIN
    SELECT RAISE(ABORT, 'change event is immutable');
END;
"""
