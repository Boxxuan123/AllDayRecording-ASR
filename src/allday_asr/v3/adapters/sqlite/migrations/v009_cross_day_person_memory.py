"""V3.5 cross-day person profile, memory, evidence, and correction schema."""

SQL = """
CREATE TABLE person_profile_revisions (
    person_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    display_name TEXT NOT NULL,
    aliases_json TEXT NOT NULL,
    relationship_labels_json TEXT NOT NULL,
    notes TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(person_id, revision),
    FOREIGN KEY(person_id) REFERENCES persons(person_id) ON DELETE RESTRICT
);

INSERT INTO person_profile_revisions (
    person_id, revision, display_name, aliases_json,
    relationship_labels_json, notes, actor, created_at
)
SELECT person_id, 1, display_name, '[]', '[]', '',
  'system:v35-profile-bootstrap', created_at FROM persons;

CREATE TABLE person_memory_entries (
    memory_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    person_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN (
        'stable_fact', 'preference', 'short_term_state', 'plan',
        'commitment', 'model_observation'
    )),
    summary TEXT NOT NULL,
    details_json TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('human', 'event_projection', 'model')),
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    confirmation_status TEXT NOT NULL CHECK(confirmation_status IN (
        'confirmed', 'unconfirmed', 'inferred'
    )),
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    status TEXT NOT NULL CHECK(status IN ('active', 'expired', 'retracted')),
    event_id TEXT,
    reminder_event_id TEXT,
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(memory_id, revision),
    CHECK(valid_until IS NULL OR valid_until > valid_from),
    FOREIGN KEY(person_id) REFERENCES persons(person_id) ON DELETE RESTRICT,
    FOREIGN KEY(event_id) REFERENCES event_current_states(event_id) ON DELETE RESTRICT,
    FOREIGN KEY(reminder_event_id) REFERENCES event_current_states(event_id)
        ON DELETE RESTRICT
);

CREATE INDEX idx_person_memory_current
ON person_memory_entries(person_id, status, kind, created_at, memory_id, revision);

CREATE INDEX idx_person_memory_event
ON person_memory_entries(event_id, person_id, memory_id, revision);

CREATE TABLE person_memory_evidence (
    link_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    memory_revision INTEGER NOT NULL CHECK(memory_revision >= 1),
    event_id TEXT,
    event_revision INTEGER CHECK(event_revision IS NULL OR event_revision >= 1),
    utterance_id TEXT,
    created_at TEXT NOT NULL,
    CHECK(event_id IS NOT NULL OR utterance_id IS NOT NULL),
    CHECK((event_id IS NULL) = (event_revision IS NULL)),
    UNIQUE(memory_id, memory_revision, event_id, event_revision, utterance_id),
    FOREIGN KEY(memory_id, memory_revision)
        REFERENCES person_memory_entries(memory_id, revision) ON DELETE RESTRICT,
    FOREIGN KEY(event_id) REFERENCES event_current_states(event_id) ON DELETE RESTRICT,
    FOREIGN KEY(utterance_id) REFERENCES utterances(utterance_id) ON DELETE RESTRICT
);

CREATE INDEX idx_person_memory_evidence_subject
ON person_memory_evidence(memory_id, memory_revision, link_id);

CREATE INDEX idx_person_memory_evidence_utterance
ON person_memory_evidence(utterance_id, memory_id, memory_revision);

CREATE TABLE person_memory_operations (
    operation_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    memory_revision INTEGER NOT NULL CHECK(memory_revision >= 1),
    kind TEXT NOT NULL CHECK(kind IN (
        'create', 'project', 'revise', 'expire', 'retract',
        'restore', 'identity_rebind'
    )),
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    reverts_operation_id TEXT UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY(memory_id, memory_revision)
        REFERENCES person_memory_entries(memory_id, revision) ON DELETE RESTRICT,
    FOREIGN KEY(reverts_operation_id) REFERENCES person_memory_operations(operation_id)
        ON DELETE RESTRICT
);

CREATE INDEX idx_person_memory_operations_memory
ON person_memory_operations(memory_id, created_at, operation_id);

CREATE TRIGGER protect_person_profile_revisions_from_update
BEFORE UPDATE ON person_profile_revisions BEGIN
    SELECT RAISE(ABORT, 'person profile revision is immutable');
END;

CREATE TRIGGER protect_person_profile_revisions_from_delete
BEFORE DELETE ON person_profile_revisions BEGIN
    SELECT RAISE(ABORT, 'person profile revision cannot be deleted directly');
END;

CREATE TRIGGER protect_person_memory_entries_from_update
BEFORE UPDATE ON person_memory_entries BEGIN
    SELECT RAISE(ABORT, 'person memory entry is immutable');
END;

CREATE TRIGGER protect_person_memory_entries_from_delete
BEFORE DELETE ON person_memory_entries BEGIN
    SELECT RAISE(ABORT, 'person memory entry cannot be deleted directly');
END;

CREATE TRIGGER protect_person_memory_evidence_from_update
BEFORE UPDATE ON person_memory_evidence BEGIN
    SELECT RAISE(ABORT, 'person memory evidence is immutable');
END;

CREATE TRIGGER protect_person_memory_evidence_from_delete
BEFORE DELETE ON person_memory_evidence BEGIN
    SELECT RAISE(ABORT, 'person memory evidence cannot be deleted directly');
END;

CREATE TRIGGER protect_person_memory_operations_from_update
BEFORE UPDATE ON person_memory_operations BEGIN
    SELECT RAISE(ABORT, 'person memory operation is immutable');
END;

CREATE TRIGGER protect_person_memory_operations_from_delete
BEFORE DELETE ON person_memory_operations BEGIN
    SELECT RAISE(ABORT, 'person memory operation cannot be deleted directly');
END;
"""
