"""V3.4 open-set speaker clusters, people, prototypes, and reversible operations."""

SQL = """
CREATE TABLE persons (
    person_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL CHECK(length(trim(display_name)) > 0),
    kind TEXT NOT NULL CHECK(kind IN ('self', 'known', 'unknown')),
    user_confirmed INTEGER NOT NULL CHECK(user_confirmed IN (0, 1)),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(kind != 'unknown' OR user_confirmed = 0)
);

CREATE UNIQUE INDEX idx_persons_one_self
ON persons(kind) WHERE kind = 'self';

CREATE TABLE speaker_cluster_runs (
    cluster_run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    producer TEXT NOT NULL,
    model TEXT NOT NULL,
    model_version TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running', 'succeeded', 'failed')),
    track_count INTEGER NOT NULL CHECK(track_count >= 0),
    error TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY(session_id) REFERENCES recording_sessions(session_id) ON DELETE RESTRICT
);

CREATE INDEX idx_speaker_cluster_runs_session
ON speaker_cluster_runs(session_id, created_at DESC, cluster_run_id);

CREATE TABLE speaker_clusters (
    cluster_id TEXT PRIMARY KEY,
    display_label TEXT NOT NULL CHECK(length(trim(display_label)) > 0),
    status TEXT NOT NULL CHECK(status IN ('active', 'merged', 'split', 'ignored')),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    suggested_person_id TEXT,
    suggestion_confidence REAL CHECK(
        suggestion_confidence IS NULL OR
        (suggestion_confidence >= 0 AND suggestion_confidence <= 1)
    ),
    merged_into_cluster_id TEXT,
    ignored_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK((suggested_person_id IS NULL) = (suggestion_confidence IS NULL)),
    FOREIGN KEY(suggested_person_id) REFERENCES persons(person_id) ON DELETE RESTRICT,
    FOREIGN KEY(merged_into_cluster_id) REFERENCES speaker_clusters(cluster_id) ON DELETE RESTRICT
);

CREATE INDEX idx_speaker_clusters_review
ON speaker_clusters(status, updated_at DESC, cluster_id);

CREATE TABLE speaker_cluster_memberships (
    membership_id TEXT PRIMARY KEY,
    cluster_id TEXT NOT NULL,
    speaker_track_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('active', 'removed')),
    source TEXT NOT NULL CHECK(source IN ('automatic', 'human')),
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    operation_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(cluster_id) REFERENCES speaker_clusters(cluster_id) ON DELETE RESTRICT,
    FOREIGN KEY(speaker_track_id) REFERENCES speaker_tracks(speaker_track_id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX idx_speaker_track_active_cluster
ON speaker_cluster_memberships(speaker_track_id) WHERE state = 'active';

CREATE INDEX idx_speaker_cluster_active_members
ON speaker_cluster_memberships(cluster_id, state, speaker_track_id);

CREATE TABLE person_cluster_links (
    link_id TEXT PRIMARY KEY,
    cluster_id TEXT NOT NULL,
    person_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'revoked')),
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    source TEXT NOT NULL CHECK(source IN ('human', 'automatic')),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    operation_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(cluster_id) REFERENCES speaker_clusters(cluster_id) ON DELETE RESTRICT,
    FOREIGN KEY(person_id) REFERENCES persons(person_id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX idx_person_cluster_active_link
ON person_cluster_links(cluster_id) WHERE status = 'active';

CREATE TABLE voice_prototypes (
    prototype_id TEXT PRIMARY KEY,
    speaker_track_id TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    person_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('candidate', 'accepted', 'revoked')),
    model TEXT NOT NULL,
    model_version TEXT NOT NULL,
    dimensions INTEGER NOT NULL CHECK(dimensions > 0),
    vector_json TEXT NOT NULL,
    representative_clips_json TEXT NOT NULL,
    quality_score REAL NOT NULL CHECK(quality_score >= 0 AND quality_score <= 1),
    human_confirmed INTEGER NOT NULL CHECK(human_confirmed IN (0, 1)),
    source_prototype_id TEXT,
    operation_id TEXT,
    created_at TEXT NOT NULL,
    CHECK(status != 'accepted' OR (person_id IS NOT NULL AND human_confirmed = 1)),
    FOREIGN KEY(speaker_track_id) REFERENCES speaker_tracks(speaker_track_id) ON DELETE RESTRICT,
    FOREIGN KEY(cluster_id) REFERENCES speaker_clusters(cluster_id) ON DELETE RESTRICT,
    FOREIGN KEY(person_id) REFERENCES persons(person_id) ON DELETE RESTRICT,
    FOREIGN KEY(source_prototype_id) REFERENCES voice_prototypes(prototype_id) ON DELETE RESTRICT
);

CREATE INDEX idx_voice_prototypes_cluster
ON voice_prototypes(cluster_id, status, created_at, prototype_id);

CREATE INDEX idx_voice_prototypes_person
ON voice_prototypes(person_id, status, created_at, prototype_id);

CREATE TABLE person_cluster_operations (
    operation_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('analyze', 'label', 'merge', 'split', 'ignore', 'undo')),
    cluster_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    reverts_operation_id TEXT UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY(cluster_id) REFERENCES speaker_clusters(cluster_id) ON DELETE RESTRICT,
    FOREIGN KEY(reverts_operation_id) REFERENCES person_cluster_operations(operation_id) ON DELETE RESTRICT
);

CREATE INDEX idx_person_cluster_operations_cluster
ON person_cluster_operations(cluster_id, created_at DESC, operation_id DESC);

CREATE TRIGGER protect_voice_prototypes_from_update
BEFORE UPDATE ON voice_prototypes BEGIN
    SELECT RAISE(ABORT, 'voice prototype is immutable');
END;

CREATE TRIGGER protect_voice_prototypes_from_delete
BEFORE DELETE ON voice_prototypes BEGIN
    SELECT RAISE(ABORT, 'voice prototype cannot be deleted');
END;

CREATE TRIGGER protect_person_cluster_operations_from_update
BEFORE UPDATE ON person_cluster_operations BEGIN
    SELECT RAISE(ABORT, 'person cluster operation is immutable');
END;

CREATE TRIGGER protect_person_cluster_operations_from_delete
BEFORE DELETE ON person_cluster_operations BEGIN
    SELECT RAISE(ABORT, 'person cluster operation cannot be deleted');
END;
"""
