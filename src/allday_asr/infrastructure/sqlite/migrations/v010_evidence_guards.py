"""Published schema migration version 10."""

SQL = """
        CREATE TABLE IF NOT EXISTS semantic_exchanges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL UNIQUE,
            session_id INTEGER NOT NULL,
            asr_run_id INTEGER NOT NULL,
            diarization_run_id INTEGER,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            request_format TEXT NOT NULL,
            response_format TEXT NOT NULL,
            request_json TEXT NOT NULL,
            request_sha256 TEXT NOT NULL,
            response_json TEXT NOT NULL,
            response_sha256 TEXT NOT NULL,
            audio_bytes_included INTEGER NOT NULL DEFAULT 0 CHECK(audio_bytes_included = 0),
            source_paths_included INTEGER NOT NULL DEFAULT 0 CHECK(source_paths_included = 0),
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES processing_runs(id) ON DELETE RESTRICT,
            FOREIGN KEY (session_id) REFERENCES recording_sessions(id) ON DELETE RESTRICT,
            FOREIGN KEY (asr_run_id) REFERENCES processing_runs(id) ON DELETE RESTRICT,
            FOREIGN KEY (diarization_run_id) REFERENCES processing_runs(id) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS semantic_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            candidate_key TEXT NOT NULL UNIQUE,
            candidate_type TEXT NOT NULL CHECK(
                candidate_type IN ('daily_summary', 'event', 'fact', 'action')
            ),
            session_start_ms INTEGER NOT NULL,
            session_end_ms INTEGER NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            confidence REAL,
            evidence_json TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, candidate_key),
            CHECK(session_end_ms > session_start_ms),
            CHECK(confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
            FOREIGN KEY (run_id) REFERENCES processing_runs(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_semantic_candidates_run_type_time
        ON semantic_candidates(run_id, candidate_type, session_start_ms);

        CREATE TABLE IF NOT EXISTS semantic_candidate_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL,
            revision_index INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('confirmed', 'rejected')),
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            note TEXT,
            content_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(candidate_id, revision_index),
            CHECK(revision_index >= 1),
            FOREIGN KEY (candidate_id) REFERENCES semantic_candidates(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_semantic_revisions_candidate_index
        ON semantic_candidate_revisions(candidate_id, revision_index DESC);

        CREATE TRIGGER IF NOT EXISTS protect_semantic_exchanges_from_update
        BEFORE UPDATE ON semantic_exchanges BEGIN
            SELECT RAISE(ABORT, 'semantic exchange cannot be changed');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_semantic_exchanges_from_delete
        BEFORE DELETE ON semantic_exchanges BEGIN
            SELECT RAISE(ABORT, 'semantic exchange cannot be deleted');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_semantic_candidates_from_update
        BEFORE UPDATE ON semantic_candidates BEGIN
            SELECT RAISE(ABORT, 'semantic candidate cannot be changed');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_semantic_candidates_from_delete
        BEFORE DELETE ON semantic_candidates BEGIN
            SELECT RAISE(ABORT, 'semantic candidate cannot be deleted');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_semantic_revisions_from_update
        BEFORE UPDATE ON semantic_candidate_revisions BEGIN
            SELECT RAISE(ABORT, 'semantic revision cannot be changed');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_semantic_revisions_from_delete
        BEFORE DELETE ON semantic_candidate_revisions BEGIN
            SELECT RAISE(ABORT, 'semantic revision cannot be deleted');
        END;
    """
