"""Published schema migration version 11."""

SQL = """
        PRAGMA foreign_keys = OFF;
        BEGIN IMMEDIATE;

        DROP TRIGGER IF EXISTS protect_diarization_turns_from_late_insert;
        DROP TRIGGER IF EXISTS protect_diarization_turn_sources_from_late_insert;
        DROP TRIGGER IF EXISTS protect_token_speaker_attributions_from_late_insert;
        DROP TRIGGER IF EXISTS protect_asr_token_sources_from_update;
        DROP TRIGGER IF EXISTS protect_diarization_turn_sources_from_update;
        DROP TRIGGER IF EXISTS protect_frozen_truth_sources_from_update;

        CREATE TABLE source_instances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_key TEXT NOT NULL UNIQUE,
            source_object_id INTEGER NOT NULL,
            source_path TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            byte_size INTEGER NOT NULL,
            recorded_at TEXT NOT NULL,
            timezone TEXT NOT NULL,
            device TEXT,
            ingest_method TEXT NOT NULL CHECK(
                ingest_method IN ('manual_file', 'watch_auto', 'watch_manual_sync')
            ),
            sample_rate INTEGER,
            sample_count INTEGER,
            integrity_status TEXT NOT NULL DEFAULT 'unverified' CHECK(
                integrity_status IN ('unverified', 'verified', 'missing', 'mismatch', 'error')
            ),
            last_verified_at TEXT,
            backup_status TEXT NOT NULL DEFAULT 'not_configured' CHECK(
                backup_status IN ('not_configured', 'pending', 'verified', 'failed')
            ),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK(sample_rate IS NULL OR sample_rate > 0),
            CHECK(sample_count IS NULL OR sample_count > 0),
            FOREIGN KEY (source_object_id) REFERENCES source_objects(id) ON DELETE RESTRICT
        );

        CREATE INDEX idx_source_instances_object
        ON source_instances(source_object_id, id);

        CREATE INDEX idx_source_instances_integrity
        ON source_instances(integrity_status, id);

        INSERT INTO source_instances (
            instance_key, source_object_id, source_path, original_filename,
            byte_size, recorded_at, timezone, device, ingest_method,
            sample_rate, sample_count, integrity_status, last_verified_at,
            backup_status, created_at, updated_at
        )
        SELECT
            'legacy-session:' || ss.session_id || ':chunk:' || ss.chunk_index,
            so.id, so.source_path, so.original_filename, COALESCE(so.byte_size, 0),
            so.recorded_at, so.timezone, so.device, so.ingest_method,
            so.sample_rate,
            CASE
                WHEN so.sample_rate IS NOT NULL
                THEN CAST(ROUND(so.duration_ms * so.sample_rate / 1000.0) AS INTEGER)
                ELSE NULL
            END,
            so.integrity_status, so.last_verified_at, so.backup_status,
            ss.created_at, ss.created_at
        FROM session_sources ss
        JOIN source_objects so ON so.id = ss.source_object_id
        ORDER BY ss.session_id, ss.chunk_index;

        CREATE TABLE session_sources_v11 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            source_object_id INTEGER NOT NULL,
            source_instance_id INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            session_start_ms INTEGER NOT NULL,
            session_end_ms INTEGER NOT NULL,
            source_start_ms INTEGER NOT NULL DEFAULT 0,
            source_end_ms INTEGER NOT NULL,
            session_start_sample INTEGER,
            session_end_sample INTEGER,
            source_start_sample INTEGER,
            source_end_sample INTEGER,
            timeline_sample_rate INTEGER,
            continuity_status TEXT NOT NULL DEFAULT 'unchecked' CHECK(
                continuity_status IN ('single', 'unchecked', 'continuous', 'gap', 'overlap')
            ),
            created_at TEXT NOT NULL,
            UNIQUE(session_id, chunk_index),
            CHECK(session_end_ms > session_start_ms),
            CHECK(source_end_ms > source_start_ms),
            CHECK(timeline_sample_rate IS NULL OR timeline_sample_rate > 0),
            FOREIGN KEY (session_id) REFERENCES recording_sessions(id) ON DELETE RESTRICT,
            FOREIGN KEY (source_object_id) REFERENCES source_objects(id) ON DELETE RESTRICT,
            FOREIGN KEY (source_instance_id) REFERENCES source_instances(id) ON DELETE RESTRICT
        );

        INSERT INTO session_sources_v11 (
            id, session_id, source_object_id, source_instance_id, chunk_index,
            session_start_ms, session_end_ms, source_start_ms, source_end_ms,
            continuity_status, created_at
        )
        SELECT
            ss.id, ss.session_id, ss.source_object_id, si.id, ss.chunk_index,
            ss.session_start_ms, ss.session_end_ms,
            ss.source_start_ms, ss.source_end_ms,
            ss.continuity_status, ss.created_at
        FROM session_sources ss
        JOIN source_instances si
          ON si.instance_key = (
              'legacy-session:' || ss.session_id || ':chunk:' || ss.chunk_index
          );

        DROP TABLE session_sources;
        ALTER TABLE session_sources_v11 RENAME TO session_sources;

        CREATE INDEX idx_session_sources_time
        ON session_sources(session_id, session_start_ms, session_end_ms);

        CREATE INDEX idx_session_sources_instance
        ON session_sources(source_instance_id, session_id);

        CREATE TABLE session_manifests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL UNIQUE,
            manifest_format TEXT NOT NULL,
            manifest_path TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL UNIQUE,
            byte_size INTEGER NOT NULL,
            parser_version TEXT NOT NULL,
            summary_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES recording_sessions(id) ON DELETE RESTRICT
        );

        CREATE TABLE processing_runs_v11 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recording_id INTEGER,
            session_id INTEGER,
            run_kind TEXT NOT NULL,
            status TEXT NOT NULL,
            config_json TEXT NOT NULL,
            config_sha256 TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            error TEXT,
            summary_json TEXT,
            artifacts_json TEXT,
            input_fingerprint TEXT,
            model_manifest_json TEXT NOT NULL DEFAULT '{}',
            pipeline_version TEXT,
            code_version TEXT,
            parent_run_id INTEGER,
            CHECK(recording_id IS NOT NULL OR session_id IS NOT NULL),
            FOREIGN KEY (recording_id) REFERENCES recordings(id) ON DELETE SET NULL,
            FOREIGN KEY (session_id) REFERENCES recording_sessions(id) ON DELETE RESTRICT
        );

        INSERT INTO processing_runs_v11 (
            id, recording_id, session_id, run_kind, status, config_json,
            config_sha256, started_at, completed_at, error, summary_json,
            artifacts_json, input_fingerprint, model_manifest_json,
            pipeline_version, code_version, parent_run_id
        )
        SELECT
            id, recording_id, session_id, run_kind, status, config_json,
            config_sha256, started_at, completed_at, error, summary_json,
            artifacts_json, input_fingerprint, model_manifest_json,
            pipeline_version, code_version, parent_run_id
        FROM processing_runs;

        DROP TABLE processing_runs;
        ALTER TABLE processing_runs_v11 RENAME TO processing_runs;

        CREATE INDEX idx_processing_runs_recording
        ON processing_runs(recording_id, id);

        CREATE INDEX idx_processing_runs_session
        ON processing_runs(session_id, id);

        ALTER TABLE processing_run_inputs
        ADD COLUMN source_instance_id INTEGER REFERENCES source_instances(id);

        ALTER TABLE asr_token_sources
        ADD COLUMN source_instance_id INTEGER REFERENCES source_instances(id);

        ALTER TABLE diarization_turn_sources
        ADD COLUMN source_instance_id INTEGER REFERENCES source_instances(id);

        ALTER TABLE truth_annotation_sources
        ADD COLUMN source_instance_id INTEGER REFERENCES source_instances(id);

        CREATE TABLE identity_reference_intervals_v11 (
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
            source_instance_id INTEGER NOT NULL,
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
                identity_label, session_id, source_instance_id,
                source_start_ms, source_end_ms
            ),
            FOREIGN KEY (session_id) REFERENCES recording_sessions(id) ON DELETE RESTRICT,
            FOREIGN KEY (source_object_id) REFERENCES source_objects(id) ON DELETE RESTRICT,
            FOREIGN KEY (source_instance_id) REFERENCES source_instances(id) ON DELETE RESTRICT
        );

        INSERT INTO identity_reference_intervals_v11 (
            id, reference_key, identity_label, decision, session_id,
            session_start_ms, session_end_ms, source_object_id,
            source_instance_id, source_sha256, source_start_ms, source_end_ms,
            provenance_kind, provenance_id, metadata_json, created_at, updated_at
        )
        SELECT
            id, reference_key, identity_label, decision, session_id,
            session_start_ms, session_end_ms, source_object_id,
            (
                SELECT ss.source_instance_id
                FROM session_sources ss
                WHERE ss.session_id = identity_reference_intervals.session_id
                  AND ss.source_object_id = identity_reference_intervals.source_object_id
                  AND identity_reference_intervals.source_start_ms >= ss.source_start_ms
                  AND identity_reference_intervals.source_end_ms <= ss.source_end_ms
                LIMIT 1
            ),
            source_sha256, source_start_ms, source_end_ms,
            provenance_kind, provenance_id, metadata_json, created_at, updated_at
        FROM identity_reference_intervals;

        DROP TABLE identity_reference_intervals;
        ALTER TABLE identity_reference_intervals_v11
        RENAME TO identity_reference_intervals;

        CREATE INDEX idx_identity_reference_label_decision
        ON identity_reference_intervals(
            identity_label, decision, session_id, session_start_ms
        );

        UPDATE processing_run_inputs
        SET source_instance_id = (
            SELECT ss.source_instance_id
            FROM processing_runs pr
            JOIN session_sources ss ON ss.session_id = pr.session_id
            WHERE pr.id = processing_run_inputs.run_id
              AND ss.source_object_id = processing_run_inputs.source_object_id
              AND ss.session_start_ms = processing_run_inputs.session_start_ms
              AND ss.session_end_ms = processing_run_inputs.session_end_ms
              AND ss.source_start_ms = processing_run_inputs.source_start_ms
              AND ss.source_end_ms = processing_run_inputs.source_end_ms
            LIMIT 1
        );

        UPDATE asr_token_sources
        SET source_instance_id = (
            SELECT ss.source_instance_id
            FROM asr_alignment_tokens token
            JOIN asr_hypotheses hypothesis ON hypothesis.id = token.hypothesis_id
            JOIN session_sources ss ON ss.session_id = hypothesis.session_id
            WHERE token.id = asr_token_sources.token_id
              AND ss.source_object_id = asr_token_sources.source_object_id
              AND asr_token_sources.source_start_ms >= ss.source_start_ms
              AND asr_token_sources.source_end_ms <= ss.source_end_ms
            LIMIT 1
        );

        UPDATE diarization_turn_sources
        SET source_instance_id = (
            SELECT ss.source_instance_id
            FROM diarization_turns turn
            JOIN session_sources ss ON ss.session_id = turn.session_id
            WHERE turn.id = diarization_turn_sources.turn_id
              AND ss.source_object_id = diarization_turn_sources.source_object_id
              AND diarization_turn_sources.source_start_ms >= ss.source_start_ms
              AND diarization_turn_sources.source_end_ms <= ss.source_end_ms
            LIMIT 1
        );

        UPDATE truth_annotation_sources
        SET source_instance_id = (
            SELECT ss.source_instance_id
            FROM truth_annotations annotation
            JOIN truth_sets truth_set ON truth_set.id = annotation.truth_set_id
            JOIN session_sources ss ON ss.session_id = truth_set.session_id
            WHERE annotation.id = truth_annotation_sources.annotation_id
              AND ss.source_object_id = truth_annotation_sources.source_object_id
              AND truth_annotation_sources.source_start_ms >= ss.source_start_ms
              AND truth_annotation_sources.source_end_ms <= ss.source_end_ms
            LIMIT 1
        );

        UPDATE identity_reference_intervals
        SET source_instance_id = (
            SELECT ss.source_instance_id
            FROM session_sources ss
            WHERE ss.session_id = identity_reference_intervals.session_id
              AND ss.source_object_id = identity_reference_intervals.source_object_id
              AND identity_reference_intervals.source_start_ms >= ss.source_start_ms
              AND identity_reference_intervals.source_end_ms <= ss.source_end_ms
            LIMIT 1
        );

        CREATE TRIGGER protect_source_instances_immutable_fields
        BEFORE UPDATE OF
            instance_key, source_object_id, source_path, original_filename,
            byte_size, recorded_at, timezone, device, ingest_method,
            sample_rate, sample_count, created_at
        ON source_instances
        BEGIN
            SELECT RAISE(ABORT, 'immutable source instance metadata cannot be changed');
        END;

        CREATE TRIGGER protect_source_instances_from_delete
        BEFORE DELETE ON source_instances
        BEGIN
            SELECT RAISE(ABORT, 'immutable source instance cannot be deleted');
        END;

        CREATE TRIGGER protect_session_sources_from_update
        BEFORE UPDATE ON session_sources
        BEGIN
            SELECT RAISE(ABORT, 'session source mapping cannot be changed');
        END;

        CREATE TRIGGER protect_session_sources_from_delete
        BEFORE DELETE ON session_sources
        BEGIN
            SELECT RAISE(ABORT, 'session source mapping cannot be deleted');
        END;

        CREATE TRIGGER protect_closed_session_from_source_insert
        BEFORE INSERT ON session_sources
        WHEN (SELECT status FROM recording_sessions WHERE id = NEW.session_id) != 'active'
        BEGIN
            SELECT RAISE(ABORT, 'cannot append source to a closed session');
        END;

        CREATE TRIGGER protect_closed_session_immutable_fields
        BEFORE UPDATE OF
            session_key, legacy_recording_id, device, recorded_at,
            timezone, duration_ms, status, created_at
        ON recording_sessions
        WHEN OLD.status = 'closed'
        BEGIN
            SELECT RAISE(ABORT, 'closed recording session cannot be changed');
        END;

        CREATE TRIGGER protect_recording_sessions_from_delete
        BEFORE DELETE ON recording_sessions
        BEGIN
            SELECT RAISE(ABORT, 'recording session cannot be deleted');
        END;

        CREATE TRIGGER protect_session_manifests_from_update
        BEFORE UPDATE ON session_manifests
        BEGIN
            SELECT RAISE(ABORT, 'session manifest cannot be changed');
        END;

        CREATE TRIGGER protect_session_manifests_from_delete
        BEFORE DELETE ON session_manifests
        BEGIN
            SELECT RAISE(ABORT, 'session manifest cannot be deleted');
        END;

        CREATE TRIGGER protect_asr_token_sources_from_update
        BEFORE UPDATE ON asr_token_sources BEGIN
            SELECT RAISE(ABORT, 'ASR token source cannot be changed');
        END;

        CREATE TRIGGER protect_diarization_turn_sources_from_update
        BEFORE UPDATE ON diarization_turn_sources BEGIN
            SELECT RAISE(ABORT, 'diarization turn source cannot be changed');
        END;

        CREATE TRIGGER protect_frozen_truth_sources_from_update
        BEFORE UPDATE ON truth_annotation_sources
        WHEN (
            SELECT ts.status
            FROM truth_sets ts
            JOIN truth_annotations ta ON ta.truth_set_id = ts.id
            WHERE ta.id = OLD.annotation_id
        ) = 'frozen'
        BEGIN
            SELECT RAISE(ABORT, 'frozen truth source reference cannot be changed');
        END;

        COMMIT;
        PRAGMA foreign_keys = ON;
    """
