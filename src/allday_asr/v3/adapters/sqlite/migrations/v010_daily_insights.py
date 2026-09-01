"""V3.6 immutable daily summaries and relationship observations."""

SQL = """
CREATE TABLE daily_summary_revisions (
    summary_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    summary_date TEXT NOT NULL,
    timezone TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    objective_json TEXT NOT NULL,
    narrative_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL CHECK(length(input_sha256) = 64),
    generation_id TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'retracted')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(summary_id, revision),
    UNIQUE(summary_date, timezone, revision),
    CHECK(period_end > period_start),
    FOREIGN KEY(generation_id) REFERENCES generation_records(generation_id)
        ON DELETE RESTRICT
);

CREATE INDEX idx_daily_summary_current
ON daily_summary_revisions(summary_date DESC, timezone, revision DESC, summary_id);

CREATE TABLE relationship_observation_revisions (
    report_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    person_id TEXT NOT NULL,
    window_days INTEGER NOT NULL CHECK(window_days IN (7, 30)),
    end_date TEXT NOT NULL,
    timezone TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    verified_facts_json TEXT NOT NULL,
    observations_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL CHECK(length(input_sha256) = 64),
    generation_id TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'retracted')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(report_id, revision),
    UNIQUE(person_id, window_days, end_date, timezone, revision),
    CHECK(period_end > period_start),
    FOREIGN KEY(person_id) REFERENCES persons(person_id) ON DELETE RESTRICT,
    FOREIGN KEY(generation_id) REFERENCES generation_records(generation_id)
        ON DELETE RESTRICT
);

CREATE INDEX idx_relationship_observation_current
ON relationship_observation_revisions(
    person_id, end_date DESC, window_days, revision DESC, report_id
);

CREATE TABLE insight_evidence (
    link_id TEXT PRIMARY KEY,
    insight_type TEXT NOT NULL CHECK(insight_type IN (
        'daily_summary', 'relationship_observation'
    )),
    insight_id TEXT NOT NULL,
    insight_revision INTEGER NOT NULL CHECK(insight_revision >= 1),
    event_id TEXT,
    event_revision INTEGER CHECK(event_revision IS NULL OR event_revision >= 1),
    utterance_id TEXT,
    utterance_revision INTEGER CHECK(
        utterance_revision IS NULL OR utterance_revision >= 1
    ),
    created_at TEXT NOT NULL,
    CHECK(event_id IS NOT NULL OR utterance_id IS NOT NULL),
    CHECK((event_id IS NULL) = (event_revision IS NULL)),
    CHECK((utterance_id IS NULL) = (utterance_revision IS NULL)),
    UNIQUE(
        insight_type, insight_id, insight_revision,
        event_id, event_revision, utterance_id, utterance_revision
    ),
    FOREIGN KEY(event_id) REFERENCES event_current_states(event_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(utterance_id) REFERENCES utterances(utterance_id)
        ON DELETE RESTRICT
);

CREATE INDEX idx_insight_evidence_subject
ON insight_evidence(insight_type, insight_id, insight_revision, link_id);

CREATE INDEX idx_insight_evidence_event
ON insight_evidence(event_id, event_revision, insight_type, insight_id);

CREATE INDEX idx_insight_evidence_utterance
ON insight_evidence(utterance_id, utterance_revision, insight_type, insight_id);

CREATE TABLE insight_operations (
    operation_id TEXT PRIMARY KEY,
    insight_type TEXT NOT NULL CHECK(insight_type IN (
        'daily_summary', 'relationship_observation'
    )),
    insight_id TEXT NOT NULL,
    insight_revision INTEGER NOT NULL CHECK(insight_revision >= 1),
    kind TEXT NOT NULL CHECK(kind IN (
        'generate', 'revise', 'retract', 'restore'
    )),
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    reverts_operation_id TEXT UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY(reverts_operation_id) REFERENCES insight_operations(operation_id)
        ON DELETE RESTRICT
);

CREATE INDEX idx_insight_operations_subject
ON insight_operations(insight_type, insight_id, created_at DESC, operation_id DESC);

CREATE TRIGGER protect_daily_summary_revisions_from_update
BEFORE UPDATE ON daily_summary_revisions BEGIN
    SELECT RAISE(ABORT, 'daily summary revision is immutable');
END;

CREATE TRIGGER protect_daily_summary_revisions_from_delete
BEFORE DELETE ON daily_summary_revisions BEGIN
    SELECT RAISE(ABORT, 'daily summary revision cannot be deleted directly');
END;

CREATE TRIGGER protect_relationship_observation_revisions_from_update
BEFORE UPDATE ON relationship_observation_revisions BEGIN
    SELECT RAISE(ABORT, 'relationship observation revision is immutable');
END;

CREATE TRIGGER protect_relationship_observation_revisions_from_delete
BEFORE DELETE ON relationship_observation_revisions BEGIN
    SELECT RAISE(ABORT, 'relationship observation revision cannot be deleted directly');
END;

CREATE TRIGGER protect_insight_evidence_from_update
BEFORE UPDATE ON insight_evidence BEGIN
    SELECT RAISE(ABORT, 'insight evidence is immutable');
END;

CREATE TRIGGER protect_insight_evidence_from_delete
BEFORE DELETE ON insight_evidence BEGIN
    SELECT RAISE(ABORT, 'insight evidence cannot be deleted directly');
END;

CREATE TRIGGER protect_insight_operations_from_update
BEFORE UPDATE ON insight_operations BEGIN
    SELECT RAISE(ABORT, 'insight operation is immutable');
END;

CREATE TRIGGER protect_insight_operations_from_delete
BEFORE DELETE ON insight_operations BEGIN
    SELECT RAISE(ABORT, 'insight operation cannot be deleted directly');
END;
"""
