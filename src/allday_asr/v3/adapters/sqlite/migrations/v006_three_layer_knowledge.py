"""V3.2 evidence, event, memory, provenance, and invalidation schema."""

SQL = """
CREATE TABLE evidence_spans (
    evidence_span_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    utterance_id TEXT,
    session_start_ms INTEGER NOT NULL CHECK(session_start_ms >= 0),
    session_end_ms INTEGER NOT NULL CHECK(session_end_ms > session_start_ms),
    asset_start_ms INTEGER NOT NULL CHECK(asset_start_ms >= 0),
    asset_end_ms INTEGER NOT NULL CHECK(asset_end_ms > asset_start_ms),
    created_at TEXT NOT NULL,
    UNIQUE(
        utterance_id, asset_id, session_start_ms, session_end_ms,
        asset_start_ms, asset_end_ms
    ),
    FOREIGN KEY(session_id) REFERENCES recording_sessions(session_id) ON DELETE RESTRICT,
    FOREIGN KEY(asset_id) REFERENCES audio_assets(asset_id) ON DELETE RESTRICT,
    FOREIGN KEY(artifact_id) REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    FOREIGN KEY(utterance_id) REFERENCES utterances(utterance_id) ON DELETE RESTRICT
);

CREATE INDEX idx_evidence_spans_session_time
ON evidence_spans(session_id, session_start_ms, session_end_ms, evidence_span_id);

CREATE INDEX idx_evidence_spans_utterance
ON evidence_spans(utterance_id, evidence_span_id);

CREATE TABLE generation_records (
    generation_id TEXT PRIMARY KEY,
    layer TEXT NOT NULL CHECK(layer IN ('evidence', 'event', 'memory')),
    producer TEXT NOT NULL,
    producer_version TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    input_scope_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL CHECK(length(input_sha256) = 64),
    generation_number INTEGER NOT NULL CHECK(generation_number >= 1),
    status TEXT NOT NULL CHECK(status IN (
        'collecting', 'succeeded', 'failed', 'stale'
    )),
    error TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(
        layer, producer, producer_version, model, prompt_version,
        extractor_version, input_sha256, generation_number
    )
);

CREATE INDEX idx_generation_records_input
ON generation_records(layer, input_sha256, generation_number, created_at);

CREATE TABLE structured_change_proposals (
    proposal_id TEXT PRIMARY KEY,
    generation_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('event_operation', 'memory_record')),
    payload_json TEXT NOT NULL,
    evidence_utterance_ids_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'accepted', 'rejected')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT,
    resolution_reason TEXT,
    CHECK(
        (status = 'pending' AND resolved_at IS NULL AND resolved_by IS NULL)
        OR
        (status != 'pending' AND resolved_at IS NOT NULL AND resolved_by IS NOT NULL)
    ),
    FOREIGN KEY(generation_id) REFERENCES generation_records(generation_id)
        ON DELETE RESTRICT
);

CREATE INDEX idx_structured_proposals_status
ON structured_change_proposals(status, created_at, proposal_id);

CREATE TABLE event_operations (
    operation_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    event_kind TEXT NOT NULL CHECK(event_kind IN (
        'task', 'request', 'commitment', 'appointment', 'decision',
        'person_fact', 'important_experience'
    )),
    operation_kind TEXT NOT NULL CHECK(operation_kind IN (
        'create', 'update', 'cancel', 'complete', 'reopen'
    )),
    event_revision INTEGER NOT NULL CHECK(event_revision >= 1),
    payload_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    proposal_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    UNIQUE(event_id, event_revision),
    FOREIGN KEY(session_id) REFERENCES recording_sessions(session_id) ON DELETE RESTRICT,
    FOREIGN KEY(generation_id) REFERENCES generation_records(generation_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(proposal_id) REFERENCES structured_change_proposals(proposal_id)
        ON DELETE RESTRICT
);

CREATE INDEX idx_event_operations_event
ON event_operations(event_id, event_revision, operation_id);

CREATE INDEX idx_event_operations_session
ON event_operations(session_id, created_at, operation_id);

CREATE TABLE event_current_states (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    event_kind TEXT NOT NULL CHECK(event_kind IN (
        'task', 'request', 'commitment', 'appointment', 'decision',
        'person_fact', 'important_experience'
    )),
    status TEXT NOT NULL CHECK(status IN ('active', 'cancelled', 'completed')),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    payload_json TEXT NOT NULL,
    latest_operation_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES recording_sessions(session_id) ON DELETE RESTRICT,
    FOREIGN KEY(latest_operation_id) REFERENCES event_operations(operation_id)
        ON DELETE RESTRICT
);

CREATE INDEX idx_event_current_session
ON event_current_states(session_id, updated_at, event_id);

CREATE TABLE memory_records (
    memory_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    session_id TEXT,
    kind TEXT NOT NULL CHECK(kind IN (
        'daily_summary', 'person_memory', 'person_fact',
        'relationship_observation', 'interaction_statistic', 'model_inference'
    )),
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    content_json TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(memory_id, version),
    FOREIGN KEY(session_id) REFERENCES recording_sessions(session_id) ON DELETE RESTRICT,
    FOREIGN KEY(generation_id) REFERENCES generation_records(generation_id)
        ON DELETE RESTRICT
);

CREATE INDEX idx_memory_records_session
ON memory_records(session_id, kind, created_at, memory_id, version);

CREATE INDEX idx_memory_records_subject
ON memory_records(subject_type, subject_id, kind, version);

CREATE TABLE evidence_links (
    link_id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    subject_revision INTEGER NOT NULL CHECK(subject_revision >= 1),
    evidence_type TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    evidence_revision INTEGER NOT NULL CHECK(evidence_revision >= 1),
    created_at TEXT NOT NULL,
    UNIQUE(
        subject_type, subject_id, subject_revision,
        evidence_type, evidence_id, evidence_revision
    )
);

CREATE INDEX idx_evidence_links_subject
ON evidence_links(subject_type, subject_id, subject_revision, link_id);

CREATE INDEX idx_evidence_links_evidence
ON evidence_links(evidence_type, evidence_id, evidence_revision, link_id);

CREATE TABLE derivation_dependencies (
    dependent_type TEXT NOT NULL,
    dependent_id TEXT NOT NULL,
    dependent_revision INTEGER NOT NULL CHECK(dependent_revision >= 1),
    input_type TEXT NOT NULL,
    input_id TEXT NOT NULL,
    input_revision INTEGER NOT NULL CHECK(input_revision >= 1),
    created_at TEXT NOT NULL,
    PRIMARY KEY(
        dependent_type, dependent_id, dependent_revision,
        input_type, input_id, input_revision
    )
);

CREATE INDEX idx_derivation_dependencies_input
ON derivation_dependencies(
    input_type, input_id, input_revision,
    dependent_type, dependent_id, dependent_revision
);

CREATE TABLE invalidation_events (
    invalidation_id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    target_revision INTEGER NOT NULL CHECK(target_revision >= 1),
    status TEXT NOT NULL CHECK(status IN ('stale', 'invalid', 'superseded')),
    reason TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL CHECK(source_revision >= 1),
    cascade_root_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(
        target_type, target_id, target_revision, status,
        source_type, source_id, source_revision
    )
);

CREATE INDEX idx_invalidation_target
ON invalidation_events(target_type, target_id, target_revision, created_at);

CREATE TABLE recompute_requests (
    request_id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    target_revision INTEGER NOT NULL CHECK(target_revision >= 1),
    invalidation_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'queued', 'running', 'succeeded', 'failed', 'cancelled'
    )),
    reason TEXT NOT NULL,
    generation_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(target_type, target_id, target_revision, invalidation_id),
    FOREIGN KEY(invalidation_id) REFERENCES invalidation_events(invalidation_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(generation_id) REFERENCES generation_records(generation_id)
        ON DELETE RESTRICT
);

CREATE INDEX idx_recompute_requests_queue
ON recompute_requests(status, created_at, request_id);

CREATE TRIGGER protect_evidence_spans_from_update
BEFORE UPDATE ON evidence_spans BEGIN
    SELECT RAISE(ABORT, 'evidence span is immutable');
END;

CREATE TRIGGER protect_evidence_spans_from_delete
BEFORE DELETE ON evidence_spans BEGIN
    SELECT RAISE(ABORT, 'evidence span cannot be deleted directly');
END;

CREATE TRIGGER protect_event_operations_from_update
BEFORE UPDATE ON event_operations BEGIN
    SELECT RAISE(ABORT, 'event operation is immutable');
END;

CREATE TRIGGER protect_event_operations_from_delete
BEFORE DELETE ON event_operations BEGIN
    SELECT RAISE(ABORT, 'event operation cannot be deleted directly');
END;

CREATE TRIGGER protect_memory_records_from_update
BEFORE UPDATE ON memory_records BEGIN
    SELECT RAISE(ABORT, 'memory record is immutable');
END;

CREATE TRIGGER protect_memory_records_from_delete
BEFORE DELETE ON memory_records BEGIN
    SELECT RAISE(ABORT, 'memory record cannot be deleted directly');
END;

CREATE TRIGGER protect_evidence_links_from_update
BEFORE UPDATE ON evidence_links BEGIN
    SELECT RAISE(ABORT, 'evidence link is immutable');
END;

CREATE TRIGGER protect_evidence_links_from_delete
BEFORE DELETE ON evidence_links BEGIN
    SELECT RAISE(ABORT, 'evidence link cannot be deleted directly');
END;

CREATE TRIGGER protect_derivation_dependencies_from_update
BEFORE UPDATE ON derivation_dependencies BEGIN
    SELECT RAISE(ABORT, 'derivation dependency is immutable');
END;

CREATE TRIGGER protect_derivation_dependencies_from_delete
BEFORE DELETE ON derivation_dependencies BEGIN
    SELECT RAISE(ABORT, 'derivation dependency cannot be deleted directly');
END;

CREATE TRIGGER protect_invalidation_events_from_update
BEFORE UPDATE ON invalidation_events BEGIN
    SELECT RAISE(ABORT, 'invalidation event is immutable');
END;

CREATE TRIGGER protect_invalidation_events_from_delete
BEFORE DELETE ON invalidation_events BEGIN
    SELECT RAISE(ABORT, 'invalidation event cannot be deleted directly');
END;
"""
