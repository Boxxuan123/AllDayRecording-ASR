"""Published schema migration version 8."""

SQL = """
        CREATE TABLE IF NOT EXISTS identity_candidate_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            candidate_id TEXT NOT NULL,
            target_identity TEXT NOT NULL,
            session_start_ms INTEGER NOT NULL,
            session_end_ms INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(
                status IN ('confirmed_target', 'rejected', 'uncertain')
            ),
            note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(run_id, candidate_id),
            CHECK(session_end_ms > session_start_ms),
            FOREIGN KEY (run_id) REFERENCES processing_runs(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_identity_candidate_reviews_run_status
        ON identity_candidate_reviews(run_id, status, session_start_ms);
    """
