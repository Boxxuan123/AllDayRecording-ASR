"""V3.3 intelligent reminder candidate, schedule, and feedback schema."""

SQL = """
CREATE TABLE reminder_candidates (
    candidate_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL UNIQUE,
    generation_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK(operation IN (
        'CREATE_TASK', 'CREATE_APPOINTMENT', 'UPDATE_EVENT',
        'CANCEL_EVENT', 'MARK_DONE', 'IGNORE'
    )),
    session_id TEXT NOT NULL,
    title TEXT,
    actor_person_id TEXT NOT NULL,
    commitment_direction TEXT NOT NULL CHECK(commitment_direction IN (
        'self_to_other', 'other_to_self', 'mutual', 'not_applicable'
    )),
    related_person_ids_json TEXT NOT NULL,
    scheduled_at TEXT,
    location TEXT,
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    needs_confirmation INTEGER NOT NULL CHECK(needs_confirmation IN (0, 1)),
    target_event_id TEXT,
    expected_revision INTEGER NOT NULL CHECK(expected_revision >= 0),
    dedup_key TEXT NOT NULL CHECK(length(dedup_key) = 64),
    status TEXT NOT NULL CHECK(status IN (
        'pending_confirmation', 'auto_applied', 'confirmed', 'modified',
        'ignored', 'duplicate', 'conflict'
    )),
    matched_event_id TEXT,
    conflict_reason TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT,
    CHECK(
        (status = 'pending_confirmation' AND resolved_at IS NULL AND resolved_by IS NULL)
        OR
        (status != 'pending_confirmation' AND resolved_at IS NOT NULL AND resolved_by IS NOT NULL)
    ),
    FOREIGN KEY(proposal_id) REFERENCES structured_change_proposals(proposal_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(generation_id) REFERENCES generation_records(generation_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(session_id) REFERENCES recording_sessions(session_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(target_event_id) REFERENCES event_current_states(event_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(matched_event_id) REFERENCES event_current_states(event_id)
        ON DELETE RESTRICT
);

CREATE INDEX idx_reminder_candidates_review
ON reminder_candidates(status, created_at, candidate_id);

CREATE INDEX idx_reminder_candidates_dedup
ON reminder_candidates(dedup_key, status, created_at, candidate_id);

CREATE TABLE reminder_schedules (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    event_revision INTEGER NOT NULL CHECK(event_revision >= 1),
    source_candidate_id TEXT NOT NULL,
    title TEXT NOT NULL,
    actor_person_id TEXT NOT NULL,
    commitment_direction TEXT NOT NULL CHECK(commitment_direction IN (
        'self_to_other', 'other_to_self', 'mutual', 'not_applicable'
    )),
    related_person_ids_json TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    location TEXT,
    status TEXT NOT NULL CHECK(status IN (
        'scheduled', 'delivered', 'completed', 'cancelled', 'stale'
    )),
    dedup_key TEXT NOT NULL CHECK(length(dedup_key) = 64),
    delivered_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(event_id) REFERENCES event_current_states(event_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(session_id) REFERENCES recording_sessions(session_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(source_candidate_id) REFERENCES reminder_candidates(candidate_id)
        ON DELETE RESTRICT
);

CREATE INDEX idx_reminder_schedules_due
ON reminder_schedules(status, scheduled_at, event_id);

CREATE INDEX idx_reminder_schedules_session
ON reminder_schedules(session_id, scheduled_at, event_id);

CREATE INDEX idx_reminder_schedules_dedup
ON reminder_schedules(dedup_key, status, event_id);

CREATE TABLE reminder_feedback (
    feedback_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN (
        'auto_apply', 'confirm', 'modify', 'ignore', 'deduplicate',
        'conflict', 'deliver'
    )),
    actor TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(candidate_id) REFERENCES reminder_candidates(candidate_id)
        ON DELETE RESTRICT
);

CREATE INDEX idx_reminder_feedback_candidate
ON reminder_feedback(candidate_id, created_at, feedback_id);

CREATE TRIGGER protect_structured_proposal_content
BEFORE UPDATE ON structured_change_proposals
WHEN NEW.proposal_id != OLD.proposal_id
  OR NEW.generation_id != OLD.generation_id
  OR NEW.kind != OLD.kind
  OR NEW.payload_json != OLD.payload_json
  OR NEW.evidence_utterance_ids_json != OLD.evidence_utterance_ids_json
  OR NEW.created_at != OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'proposal content is immutable');
END;

CREATE TRIGGER protect_structured_proposals_from_delete
BEFORE DELETE ON structured_change_proposals BEGIN
    SELECT RAISE(ABORT, 'proposal cannot be deleted directly');
END;

CREATE TRIGGER protect_reminder_candidate_content
BEFORE UPDATE ON reminder_candidates
WHEN NEW.candidate_id != OLD.candidate_id
  OR NEW.proposal_id != OLD.proposal_id
  OR NEW.generation_id != OLD.generation_id
  OR NEW.operation != OLD.operation
  OR NEW.session_id != OLD.session_id
  OR NEW.title IS NOT OLD.title
  OR NEW.actor_person_id != OLD.actor_person_id
  OR NEW.commitment_direction != OLD.commitment_direction
  OR NEW.related_person_ids_json != OLD.related_person_ids_json
  OR NEW.scheduled_at IS NOT OLD.scheduled_at
  OR NEW.location IS NOT OLD.location
  OR NEW.confidence != OLD.confidence
  OR NEW.needs_confirmation != OLD.needs_confirmation
  OR NEW.target_event_id IS NOT OLD.target_event_id
  OR NEW.expected_revision != OLD.expected_revision
  OR NEW.dedup_key != OLD.dedup_key
  OR NEW.created_at != OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'reminder candidate content is immutable');
END;

CREATE TRIGGER protect_reminder_candidates_from_delete
BEFORE DELETE ON reminder_candidates BEGIN
    SELECT RAISE(ABORT, 'reminder candidate cannot be deleted directly');
END;

CREATE TRIGGER protect_reminder_feedback_from_update
BEFORE UPDATE ON reminder_feedback BEGIN
    SELECT RAISE(ABORT, 'reminder feedback is immutable');
END;

CREATE TRIGGER protect_reminder_feedback_from_delete
BEFORE DELETE ON reminder_feedback BEGIN
    SELECT RAISE(ABORT, 'reminder feedback cannot be deleted directly');
END;

ALTER TABLE sync_cursors RENAME TO sync_cursors_v32;

CREATE TABLE sync_cursors (
    device_id TEXT PRIMARY KEY,
    projection_version INTEGER NOT NULL CHECK(projection_version = 4),
    acknowledged_sequence INTEGER NOT NULL CHECK(acknowledged_sequence >= 0),
    updated_at TEXT NOT NULL,
    FOREIGN KEY(device_id) REFERENCES devices(device_id) ON DELETE RESTRICT
);

INSERT INTO sync_cursors (
    device_id, projection_version, acknowledged_sequence, updated_at
)
SELECT device_id, 4, 0, updated_at FROM sync_cursors_v32;

DROP TABLE sync_cursors_v32;
"""
