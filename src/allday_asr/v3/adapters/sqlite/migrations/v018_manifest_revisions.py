"""Preserve immutable original manifests while allowing audited append repairs."""

SQL = """
CREATE TABLE session_manifest_revisions (
    session_id TEXT NOT NULL,
    input_revision INTEGER NOT NULL CHECK(input_revision >= 2),
    sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
    storage_ref TEXT NOT NULL,
    entries_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(session_id, input_revision),
    UNIQUE(session_id, sha256),
    FOREIGN KEY(session_id) REFERENCES recording_sessions(session_id) ON DELETE RESTRICT
);
"""
