"""Published schema migration version 9."""

SQL = """
        CREATE TABLE IF NOT EXISTS identity_reference_intervals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reference_key TEXT NOT NULL UNIQUE,
            identity_label TEXT NOT NULL,
            decision TEXT NOT NULL CHECK(
                decision IN ('confirmed_target', 'rejected', 'uncertain')
            ),
            session_id INTEGER NOT NULL,
            session_start_ms INTEGER NOT NULL,
            session_end_ms INTEGER NOT NULL,
            source_object_id INTEGER NOT NULL,
            source_sha256 TEXT NOT NULL,
            source_start_ms INTEGER NOT NULL,
            source_end_ms INTEGER NOT NULL,
            provenance_kind TEXT NOT NULL CHECK(
                provenance_kind IN ('truth', 'v2d3_review')
            ),
            provenance_id INTEGER NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK(session_end_ms > session_start_ms),
            CHECK(source_end_ms > source_start_ms),
            UNIQUE(
                identity_label, session_id, source_object_id,
                source_start_ms, source_end_ms
            ),
            FOREIGN KEY (session_id) REFERENCES recording_sessions(id) ON DELETE RESTRICT,
            FOREIGN KEY (source_object_id) REFERENCES source_objects(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_identity_reference_label_decision
        ON identity_reference_intervals(
            identity_label, decision, session_id, session_start_ms
        );
    """
