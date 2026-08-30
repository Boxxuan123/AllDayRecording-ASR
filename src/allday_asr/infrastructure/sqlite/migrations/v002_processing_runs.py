"""Published schema migration version 2."""

SQL = """
        CREATE TABLE IF NOT EXISTS processing_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recording_id INTEGER NOT NULL,
            run_kind TEXT NOT NULL,
            status TEXT NOT NULL,
            config_json TEXT NOT NULL,
            config_sha256 TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            error TEXT,
            summary_json TEXT,
            artifacts_json TEXT,
            FOREIGN KEY (recording_id) REFERENCES recordings(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_processing_runs_recording
        ON processing_runs(recording_id, id);

        CREATE TABLE IF NOT EXISTS evaluation_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recording_id INTEGER NOT NULL,
            truth_path TEXT NOT NULL,
            truth_sha256 TEXT NOT NULL,
            config_json TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            report_json_path TEXT NOT NULL,
            report_markdown_path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (recording_id) REFERENCES recordings(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_evaluation_runs_recording
        ON evaluation_runs(recording_id, id);
    """
