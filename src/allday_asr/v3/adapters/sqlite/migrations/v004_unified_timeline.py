"""V3.1 canonical utterance time and correction baseline."""

SQL = """
ALTER TABLE utterances RENAME TO utterances_v30;

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
    evidence_json, revision, status, created_at, updated_at
)
SELECT
    u.utterance_id, u.session_id, u.run_id, u.source_artifact_id,
    u.speaker_track_id,
    COALESCE(
        json_extract((
            SELECT payload_json FROM change_events
            WHERE resource_type = 'utterance'
              AND resource_id = u.utterance_id
              AND operation = 'upsert'
            ORDER BY sequence LIMIT 1
        ), '$.speaker_track_id'),
        u.speaker_track_id
    ),
    u.ordinal,
    u.start_ms, u.end_ms,
    strftime(
        '%Y-%m-%dT%H:%M:%fZ', s.captured_start,
        '+' || (u.start_ms / 1000.0) || ' seconds'
    ),
    strftime(
        '%Y-%m-%dT%H:%M:%fZ', s.captured_start,
        '+' || (u.end_ms / 1000.0) || ' seconds'
    ),
    u.text,
    COALESCE(
        json_extract((
            SELECT payload_json FROM change_events
            WHERE resource_type = 'utterance'
              AND resource_id = u.utterance_id
              AND operation = 'upsert'
            ORDER BY sequence LIMIT 1
        ), '$.text'),
        u.text
    ),
    u.evidence_json, u.revision, u.status,
    u.created_at, u.updated_at
FROM utterances_v30 u
JOIN recording_sessions s ON s.session_id = u.session_id;

DROP TABLE utterances_v30;

CREATE INDEX idx_utterances_session_time
ON utterances(session_id, start_ms, end_ms, utterance_id);

ALTER TABLE sync_cursors RENAME TO sync_cursors_v30;

CREATE TABLE sync_cursors (
    device_id TEXT PRIMARY KEY,
    projection_version INTEGER NOT NULL CHECK(projection_version = 2),
    acknowledged_sequence INTEGER NOT NULL CHECK(acknowledged_sequence >= 0),
    updated_at TEXT NOT NULL,
    FOREIGN KEY(device_id) REFERENCES devices(device_id) ON DELETE RESTRICT
);

INSERT INTO sync_cursors (
    device_id, projection_version, acknowledged_sequence, updated_at
)
SELECT device_id, 2, 0, updated_at FROM sync_cursors_v30;

DROP TABLE sync_cursors_v30;

DROP TRIGGER protect_change_events_from_update;

UPDATE change_events
SET payload_json = json_set(
    payload_json,
    '$.original_speaker_track_id', (
        SELECT original_speaker_track_id FROM utterances
        WHERE utterances.utterance_id = change_events.resource_id
    ),
    '$.original_speaker_label', (
        SELECT t.label FROM utterances u
        LEFT JOIN speaker_tracks t
          ON t.speaker_track_id = u.original_speaker_track_id
        WHERE u.utterance_id = change_events.resource_id
    ),
    '$.start_at', (
        SELECT start_at FROM utterances
        WHERE utterances.utterance_id = change_events.resource_id
    ),
    '$.end_at', (
        SELECT end_at FROM utterances
        WHERE utterances.utterance_id = change_events.resource_id
    ),
    '$.original_text', (
        SELECT original_text FROM utterances
        WHERE utterances.utterance_id = change_events.resource_id
    )
)
WHERE resource_type = 'utterance' AND operation = 'upsert';

CREATE TRIGGER protect_change_events_from_update
BEFORE UPDATE ON change_events BEGIN
    SELECT RAISE(ABORT, 'change event is immutable');
END;
"""
