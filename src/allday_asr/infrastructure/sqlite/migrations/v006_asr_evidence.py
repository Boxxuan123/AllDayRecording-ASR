"""Published schema migration version 6."""

SQL = """
        CREATE TABLE IF NOT EXISTS asr_hypotheses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hypothesis_key TEXT NOT NULL UNIQUE,
            run_id INTEGER NOT NULL,
            session_id INTEGER NOT NULL,
            window_index INTEGER NOT NULL,
            hypothesis_role TEXT NOT NULL CHECK(
                hypothesis_role IN ('primary', 'secondary')
            ),
            core_start_ms INTEGER NOT NULL,
            core_end_ms INTEGER NOT NULL,
            analysis_start_ms INTEGER NOT NULL,
            analysis_end_ms INTEGER NOT NULL,
            model_id TEXT NOT NULL,
            model_revision TEXT,
            backend TEXT NOT NULL,
            language TEXT,
            text TEXT NOT NULL,
            parameters_json TEXT NOT NULL,
            raw_response_json TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, window_index, hypothesis_role),
            CHECK(core_end_ms > core_start_ms),
            CHECK(analysis_end_ms > analysis_start_ms),
            CHECK(analysis_start_ms <= core_start_ms),
            CHECK(analysis_end_ms >= core_end_ms),
            FOREIGN KEY (run_id) REFERENCES processing_runs(id) ON DELETE RESTRICT,
            FOREIGN KEY (session_id) REFERENCES recording_sessions(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_asr_hypotheses_run_window
        ON asr_hypotheses(run_id, window_index, hypothesis_role);

        CREATE TABLE IF NOT EXISTS asr_alignment_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hypothesis_id INTEGER NOT NULL,
            token_index INTEGER NOT NULL,
            text TEXT NOT NULL,
            session_start_ms INTEGER NOT NULL,
            session_end_ms INTEGER NOT NULL,
            analysis_start_ms INTEGER NOT NULL,
            analysis_end_ms INTEGER NOT NULL,
            kept_in_core INTEGER NOT NULL CHECK(kept_in_core IN (0, 1)),
            confidence REAL,
            alignment_model_id TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE(hypothesis_id, token_index),
            CHECK(session_end_ms > session_start_ms),
            CHECK(analysis_end_ms > analysis_start_ms),
            FOREIGN KEY (hypothesis_id) REFERENCES asr_hypotheses(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_asr_alignment_tokens_time
        ON asr_alignment_tokens(hypothesis_id, session_start_ms, session_end_ms);

        CREATE TABLE IF NOT EXISTS asr_token_sources (
            token_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            source_object_id INTEGER NOT NULL,
            source_sha256 TEXT NOT NULL,
            source_start_ms INTEGER NOT NULL,
            source_end_ms INTEGER NOT NULL,
            PRIMARY KEY (token_id, position),
            CHECK(source_end_ms > source_start_ms),
            FOREIGN KEY (token_id) REFERENCES asr_alignment_tokens(id) ON DELETE RESTRICT,
            FOREIGN KEY (source_object_id) REFERENCES source_objects(id) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS asr_disagreements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            window_index INTEGER NOT NULL,
            primary_hypothesis_id INTEGER NOT NULL,
            secondary_hypothesis_id INTEGER NOT NULL,
            session_start_ms INTEGER NOT NULL,
            session_end_ms INTEGER NOT NULL,
            normalized_distance REAL NOT NULL,
            priority TEXT NOT NULL CHECK(priority IN ('low', 'medium', 'high')),
            details_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, window_index),
            CHECK(session_end_ms > session_start_ms),
            CHECK(normalized_distance >= 0.0 AND normalized_distance <= 1.0),
            FOREIGN KEY (run_id) REFERENCES processing_runs(id) ON DELETE RESTRICT,
            FOREIGN KEY (primary_hypothesis_id) REFERENCES asr_hypotheses(id) ON DELETE RESTRICT,
            FOREIGN KEY (secondary_hypothesis_id) REFERENCES asr_hypotheses(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_asr_disagreements_run_priority
        ON asr_disagreements(run_id, priority, window_index);

        CREATE TRIGGER IF NOT EXISTS protect_asr_hypotheses_from_update
        BEFORE UPDATE ON asr_hypotheses BEGIN
            SELECT RAISE(ABORT, 'ASR hypothesis cannot be changed');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_asr_hypotheses_from_delete
        BEFORE DELETE ON asr_hypotheses BEGIN
            SELECT RAISE(ABORT, 'ASR hypothesis cannot be deleted');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_asr_tokens_from_update
        BEFORE UPDATE ON asr_alignment_tokens BEGIN
            SELECT RAISE(ABORT, 'ASR token cannot be changed');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_asr_tokens_from_delete
        BEFORE DELETE ON asr_alignment_tokens BEGIN
            SELECT RAISE(ABORT, 'ASR token cannot be deleted');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_asr_token_sources_from_update
        BEFORE UPDATE ON asr_token_sources BEGIN
            SELECT RAISE(ABORT, 'ASR token source cannot be changed');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_asr_token_sources_from_delete
        BEFORE DELETE ON asr_token_sources BEGIN
            SELECT RAISE(ABORT, 'ASR token source cannot be deleted');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_asr_disagreements_from_update
        BEFORE UPDATE ON asr_disagreements BEGIN
            SELECT RAISE(ABORT, 'ASR disagreement cannot be changed');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_asr_disagreements_from_delete
        BEFORE DELETE ON asr_disagreements BEGIN
            SELECT RAISE(ABORT, 'ASR disagreement cannot be deleted');
        END;
    """
