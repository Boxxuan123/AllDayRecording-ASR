"""Published schema migration version 14."""

SQL = """
        CREATE TABLE IF NOT EXISTS v2d1_identity_labels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            candidate_id TEXT NOT NULL,
            identity_label TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(run_id, candidate_id),
            FOREIGN KEY (run_id, candidate_id)
                REFERENCES v2d1_candidate_reviews(run_id, candidate_id)
                ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_v2d1_identity_labels_run_identity
        ON v2d1_identity_labels(run_id, identity_label, candidate_id);
    """
