"""Published schema migration version 15."""

SQL = """
        CREATE TABLE IF NOT EXISTS manual_identity_annotations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            annotation_key TEXT NOT NULL UNIQUE,
            session_id INTEGER NOT NULL,
            diarization_run_id INTEGER NOT NULL,
            session_start_ms INTEGER NOT NULL,
            session_end_ms INTEGER NOT NULL,
            identity_label TEXT NOT NULL,
            anonymous_speaker_label TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active', 'retracted')),
            note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(
                session_id, diarization_run_id,
                session_start_ms, session_end_ms
            ),
            CHECK(session_end_ms > session_start_ms),
            FOREIGN KEY (session_id)
                REFERENCES recording_sessions(id) ON DELETE RESTRICT,
            FOREIGN KEY (diarization_run_id)
                REFERENCES processing_runs(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_manual_identity_annotations_session_status
        ON manual_identity_annotations(
            session_id, diarization_run_id, status,
            identity_label, session_start_ms
        );
    """
