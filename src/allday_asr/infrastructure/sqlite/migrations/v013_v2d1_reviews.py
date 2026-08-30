"""Published schema migration version 13."""

SQL = """
        CREATE TABLE IF NOT EXISTS v2d1_candidate_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            candidate_id TEXT NOT NULL,
            session_start_ms INTEGER NOT NULL,
            session_end_ms INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(
                status IN ('confirmed_speech', 'rejected', 'uncertain')
            ),
            note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(run_id, candidate_id),
            CHECK(session_end_ms > session_start_ms),
            FOREIGN KEY (run_id) REFERENCES processing_runs(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_v2d1_candidate_reviews_run_status
        ON v2d1_candidate_reviews(run_id, status, session_start_ms);

        CREATE TABLE IF NOT EXISTS v2d1_review_completions (
            run_id INTEGER PRIMARY KEY,
            candidate_count INTEGER NOT NULL,
            reviewed_count INTEGER NOT NULL,
            completed_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK(candidate_count >= 0),
            CHECK(reviewed_count >= 0),
            CHECK(reviewed_count <= candidate_count),
            FOREIGN KEY (run_id) REFERENCES processing_runs(id) ON DELETE RESTRICT
        );
    """
