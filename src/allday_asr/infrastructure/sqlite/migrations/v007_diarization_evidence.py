"""Published schema migration version 7."""

SQL = """
        CREATE TABLE IF NOT EXISTS diarization_turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            turn_key TEXT NOT NULL UNIQUE,
            run_id INTEGER NOT NULL,
            session_id INTEGER NOT NULL,
            turn_index INTEGER NOT NULL,
            turn_kind TEXT NOT NULL CHECK(turn_kind IN ('regular', 'exclusive')),
            speaker_label TEXT NOT NULL,
            session_start_ms INTEGER NOT NULL,
            session_end_ms INTEGER NOT NULL,
            confidence REAL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            content_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, turn_kind, turn_index),
            CHECK(session_end_ms > session_start_ms),
            CHECK(confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
            FOREIGN KEY (run_id) REFERENCES processing_runs(id) ON DELETE RESTRICT,
            FOREIGN KEY (session_id) REFERENCES recording_sessions(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_diarization_turns_run_time
        ON diarization_turns(run_id, turn_kind, session_start_ms, session_end_ms);

        CREATE TABLE IF NOT EXISTS diarization_turn_sources (
            turn_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            source_object_id INTEGER NOT NULL,
            source_sha256 TEXT NOT NULL,
            source_start_ms INTEGER NOT NULL,
            source_end_ms INTEGER NOT NULL,
            PRIMARY KEY (turn_id, position),
            CHECK(source_end_ms > source_start_ms),
            FOREIGN KEY (turn_id) REFERENCES diarization_turns(id) ON DELETE RESTRICT,
            FOREIGN KEY (source_object_id) REFERENCES source_objects(id) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS token_speaker_attributions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attribution_key TEXT NOT NULL UNIQUE,
            run_id INTEGER NOT NULL,
            asr_run_id INTEGER NOT NULL,
            token_id INTEGER NOT NULL,
            speaker_label TEXT,
            attribution_kind TEXT NOT NULL CHECK(
                attribution_kind IN ('primary', 'overlap', 'uncertain', 'none')
            ),
            overlap_ms INTEGER NOT NULL DEFAULT 0,
            overlap_ratio REAL NOT NULL DEFAULT 0.0,
            rank INTEGER NOT NULL,
            confidence REAL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            content_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, token_id, rank),
            CHECK(overlap_ms >= 0),
            CHECK(overlap_ratio >= 0.0 AND overlap_ratio <= 1.0),
            CHECK(rank >= 0),
            CHECK(confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
            CHECK(
                (attribution_kind = 'none' AND speaker_label IS NULL)
                OR (attribution_kind != 'none' AND speaker_label IS NOT NULL)
            ),
            FOREIGN KEY (run_id) REFERENCES processing_runs(id) ON DELETE RESTRICT,
            FOREIGN KEY (asr_run_id) REFERENCES processing_runs(id) ON DELETE RESTRICT,
            FOREIGN KEY (token_id) REFERENCES asr_alignment_tokens(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_token_speaker_attributions_run_token
        ON token_speaker_attributions(run_id, token_id, rank);

        CREATE TRIGGER IF NOT EXISTS protect_diarization_turns_from_update
        BEFORE UPDATE ON diarization_turns BEGIN
            SELECT RAISE(ABORT, 'diarization turn cannot be changed');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_diarization_turns_from_delete
        BEFORE DELETE ON diarization_turns BEGIN
            SELECT RAISE(ABORT, 'diarization turn cannot be deleted');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_diarization_turn_sources_from_update
        BEFORE UPDATE ON diarization_turn_sources BEGIN
            SELECT RAISE(ABORT, 'diarization turn source cannot be changed');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_diarization_turn_sources_from_delete
        BEFORE DELETE ON diarization_turn_sources BEGIN
            SELECT RAISE(ABORT, 'diarization turn source cannot be deleted');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_token_speaker_attributions_from_update
        BEFORE UPDATE ON token_speaker_attributions BEGIN
            SELECT RAISE(ABORT, 'token speaker attribution cannot be changed');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_token_speaker_attributions_from_delete
        BEFORE DELETE ON token_speaker_attributions BEGIN
            SELECT RAISE(ABORT, 'token speaker attribution cannot be deleted');
        END;
    """
