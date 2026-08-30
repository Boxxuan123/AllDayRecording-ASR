"""Published schema migration version 5."""

SQL = """
        CREATE TABLE IF NOT EXISTS truth_sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            truth_key TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            session_id INTEGER NOT NULL,
            parent_truth_set_id INTEGER,
            format_version TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('draft', 'frozen')),
            scope_start_ms INTEGER NOT NULL,
            scope_end_ms INTEGER NOT NULL,
            input_fingerprint TEXT NOT NULL,
            completeness_json TEXT NOT NULL,
            truth_path TEXT NOT NULL,
            truth_sha256 TEXT NOT NULL,
            provenance_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            frozen_at TEXT,
            CHECK(scope_end_ms > scope_start_ms),
            FOREIGN KEY (session_id) REFERENCES recording_sessions(id) ON DELETE RESTRICT,
            FOREIGN KEY (parent_truth_set_id) REFERENCES truth_sets(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_truth_sets_session
        ON truth_sets(session_id, id);

        CREATE TABLE IF NOT EXISTS truth_annotations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            truth_set_id INTEGER NOT NULL,
            annotation_key TEXT NOT NULL,
            annotation_kind TEXT NOT NULL CHECK(annotation_kind IN (
                'speech', 'non_speech', 'transcript', 'speaker', 'identity',
                'overlap', 'alignment_token', 'entity', 'uncertain'
            )),
            session_start_ms INTEGER NOT NULL,
            session_end_ms INTEGER NOT NULL,
            label TEXT,
            text TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            legacy_segment_id INTEGER,
            created_at TEXT NOT NULL,
            UNIQUE(truth_set_id, annotation_key),
            CHECK(session_end_ms > session_start_ms),
            FOREIGN KEY (truth_set_id) REFERENCES truth_sets(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_truth_annotations_time
        ON truth_annotations(truth_set_id, annotation_kind, session_start_ms, session_end_ms);

        CREATE TABLE IF NOT EXISTS truth_annotation_sources (
            annotation_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            source_object_id INTEGER NOT NULL,
            source_sha256 TEXT NOT NULL,
            source_start_ms INTEGER NOT NULL,
            source_end_ms INTEGER NOT NULL,
            PRIMARY KEY (annotation_id, position),
            CHECK(source_end_ms > source_start_ms),
            FOREIGN KEY (annotation_id) REFERENCES truth_annotations(id) ON DELETE RESTRICT,
            FOREIGN KEY (source_object_id) REFERENCES source_objects(id) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS benchmark_prediction_sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prediction_key TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            session_id INTEGER NOT NULL,
            processing_run_id INTEGER,
            input_fingerprint TEXT NOT NULL,
            adapter TEXT NOT NULL,
            model_manifest_json TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('draft', 'frozen')),
            created_at TEXT NOT NULL,
            frozen_at TEXT,
            FOREIGN KEY (session_id) REFERENCES recording_sessions(id) ON DELETE RESTRICT,
            FOREIGN KEY (processing_run_id) REFERENCES processing_runs(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_benchmark_prediction_sets_session
        ON benchmark_prediction_sets(session_id, id);

        CREATE TABLE IF NOT EXISTS benchmark_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prediction_set_id INTEGER NOT NULL,
            prediction_key TEXT NOT NULL,
            prediction_kind TEXT NOT NULL CHECK(prediction_kind IN (
                'speech', 'transcript', 'speaker', 'identity',
                'overlap', 'alignment_token', 'entity'
            )),
            session_start_ms INTEGER NOT NULL,
            session_end_ms INTEGER NOT NULL,
            label TEXT,
            text TEXT,
            confidence REAL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE(prediction_set_id, prediction_key),
            CHECK(session_end_ms > session_start_ms),
            FOREIGN KEY (prediction_set_id)
                REFERENCES benchmark_prediction_sets(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_benchmark_predictions_time
        ON benchmark_predictions(
            prediction_set_id, prediction_kind, session_start_ms, session_end_ms
        );

        CREATE TABLE IF NOT EXISTS benchmark_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            truth_set_id INTEGER NOT NULL,
            prediction_set_id INTEGER NOT NULL,
            config_json TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            details_json TEXT NOT NULL,
            report_json_path TEXT NOT NULL,
            report_markdown_path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (truth_set_id) REFERENCES truth_sets(id) ON DELETE RESTRICT,
            FOREIGN KEY (prediction_set_id)
                REFERENCES benchmark_prediction_sets(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_benchmark_runs_truth
        ON benchmark_runs(truth_set_id, id);

        CREATE TRIGGER IF NOT EXISTS protect_frozen_truth_sets_from_update
        BEFORE UPDATE ON truth_sets WHEN OLD.status = 'frozen'
        BEGIN
            SELECT RAISE(ABORT, 'frozen truth set cannot be changed');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_truth_sets_from_delete
        BEFORE DELETE ON truth_sets
        BEGIN
            SELECT RAISE(ABORT, 'truth set cannot be deleted');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_frozen_truth_annotations_from_update
        BEFORE UPDATE ON truth_annotations
        WHEN (SELECT status FROM truth_sets WHERE id = OLD.truth_set_id) = 'frozen'
        BEGIN
            SELECT RAISE(ABORT, 'frozen truth annotation cannot be changed');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_frozen_truth_annotations_from_insert
        BEFORE INSERT ON truth_annotations
        WHEN (SELECT status FROM truth_sets WHERE id = NEW.truth_set_id) = 'frozen'
        BEGIN
            SELECT RAISE(ABORT, 'cannot append to frozen truth set');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_frozen_truth_annotations_from_delete
        BEFORE DELETE ON truth_annotations
        WHEN (SELECT status FROM truth_sets WHERE id = OLD.truth_set_id) = 'frozen'
        BEGIN
            SELECT RAISE(ABORT, 'frozen truth annotation cannot be deleted');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_frozen_truth_sources_from_update
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

        CREATE TRIGGER IF NOT EXISTS protect_frozen_truth_sources_from_insert
        BEFORE INSERT ON truth_annotation_sources
        WHEN (
            SELECT ts.status
            FROM truth_sets ts
            JOIN truth_annotations ta ON ta.truth_set_id = ts.id
            WHERE ta.id = NEW.annotation_id
        ) = 'frozen'
        BEGIN
            SELECT RAISE(ABORT, 'cannot append source reference to frozen truth set');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_frozen_truth_sources_from_delete
        BEFORE DELETE ON truth_annotation_sources
        WHEN (
            SELECT ts.status
            FROM truth_sets ts
            JOIN truth_annotations ta ON ta.truth_set_id = ts.id
            WHERE ta.id = OLD.annotation_id
        ) = 'frozen'
        BEGIN
            SELECT RAISE(ABORT, 'frozen truth source reference cannot be deleted');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_frozen_prediction_sets_from_update
        BEFORE UPDATE ON benchmark_prediction_sets WHEN OLD.status = 'frozen'
        BEGIN
            SELECT RAISE(ABORT, 'benchmark prediction set cannot be changed');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_prediction_sets_from_delete
        BEFORE DELETE ON benchmark_prediction_sets
        BEGIN
            SELECT RAISE(ABORT, 'benchmark prediction set cannot be deleted');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_predictions_from_update
        BEFORE UPDATE ON benchmark_predictions
        BEGIN
            SELECT RAISE(ABORT, 'benchmark prediction cannot be changed');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_predictions_from_insert
        BEFORE INSERT ON benchmark_predictions
        WHEN (
            SELECT status FROM benchmark_prediction_sets
            WHERE id = NEW.prediction_set_id
        ) = 'frozen'
        BEGIN
            SELECT RAISE(ABORT, 'cannot append to frozen benchmark prediction set');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_predictions_from_delete
        BEFORE DELETE ON benchmark_predictions
        BEGIN
            SELECT RAISE(ABORT, 'benchmark prediction cannot be deleted');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_benchmark_runs_from_update
        BEFORE UPDATE ON benchmark_runs
        BEGIN
            SELECT RAISE(ABORT, 'benchmark run cannot be changed');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_benchmark_runs_from_delete
        BEFORE DELETE ON benchmark_runs
        BEGIN
            SELECT RAISE(ABORT, 'benchmark run cannot be deleted');
        END;
    """
