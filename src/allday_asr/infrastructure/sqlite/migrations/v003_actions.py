"""Published schema migration version 3."""

SQL = """
        CREATE TABLE IF NOT EXISTS action_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recording_id INTEGER NOT NULL,
            candidate_key TEXT NOT NULL UNIQUE,
            candidate_type TEXT NOT NULL CHECK(candidate_type IN ('schedule', 'todo')),
            status TEXT NOT NULL CHECK(status IN ('pending', 'confirmed', 'dismissed')),
            start_ms INTEGER NOT NULL,
            end_ms INTEGER NOT NULL,
            source_segment_ids_json TEXT NOT NULL,
            title TEXT NOT NULL,
            scheduled_at TEXT,
            time_text TEXT,
            location TEXT,
            participants_json TEXT NOT NULL,
            confidence REAL NOT NULL,
            evidence_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (recording_id) REFERENCES recordings(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_action_candidates_recording_status
        ON action_candidates(recording_id, status, start_ms);
    """
