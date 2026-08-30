"""Published schema migration version 4."""

SQL = """
        CREATE TABLE IF NOT EXISTS source_objects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sha256 TEXT NOT NULL UNIQUE,
            source_path TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            byte_size INTEGER,
            container TEXT,
            codec TEXT,
            sample_rate INTEGER,
            channels INTEGER,
            bit_rate INTEGER,
            encoder TEXT,
            device TEXT,
            recorded_at TEXT NOT NULL,
            timezone TEXT NOT NULL,
            duration_ms INTEGER NOT NULL,
            ingest_method TEXT NOT NULL CHECK(
                ingest_method IN ('manual_file', 'watch_auto', 'watch_manual_sync')
            ),
            storage_class TEXT NOT NULL DEFAULT 'original_permanent' CHECK(
                storage_class = 'original_permanent'
            ),
            integrity_status TEXT NOT NULL DEFAULT 'unverified' CHECK(
                integrity_status IN ('unverified', 'verified', 'missing', 'mismatch', 'error')
            ),
            last_verified_at TEXT,
            backup_status TEXT NOT NULL DEFAULT 'not_configured' CHECK(
                backup_status IN ('not_configured', 'pending', 'verified', 'failed')
            ),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_source_objects_integrity
        ON source_objects(integrity_status, id);

        CREATE TRIGGER IF NOT EXISTS protect_source_objects_immutable_fields
        BEFORE UPDATE OF
            sha256, source_path, original_filename, byte_size, container, codec,
            sample_rate, channels, bit_rate, encoder, device, recorded_at,
            timezone, duration_ms, ingest_method, storage_class, created_at
        ON source_objects
        BEGIN
            SELECT RAISE(ABORT, 'immutable source metadata cannot be changed');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_source_objects_from_delete
        BEFORE DELETE ON source_objects
        BEGIN
            SELECT RAISE(ABORT, 'immutable source object cannot be deleted');
        END;

        CREATE TABLE IF NOT EXISTS recording_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_key TEXT NOT NULL UNIQUE,
            legacy_recording_id INTEGER UNIQUE,
            device TEXT,
            recorded_at TEXT NOT NULL,
            timezone TEXT NOT NULL,
            duration_ms INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'closed')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (legacy_recording_id) REFERENCES recordings(id) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS session_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            source_object_id INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            session_start_ms INTEGER NOT NULL,
            session_end_ms INTEGER NOT NULL,
            source_start_ms INTEGER NOT NULL DEFAULT 0,
            source_end_ms INTEGER NOT NULL,
            continuity_status TEXT NOT NULL DEFAULT 'unchecked' CHECK(
                continuity_status IN ('single', 'unchecked', 'continuous', 'gap', 'overlap')
            ),
            created_at TEXT NOT NULL,
            UNIQUE(session_id, chunk_index),
            UNIQUE(session_id, source_object_id),
            CHECK(session_end_ms > session_start_ms),
            CHECK(source_end_ms > source_start_ms),
            FOREIGN KEY (session_id) REFERENCES recording_sessions(id) ON DELETE RESTRICT,
            FOREIGN KEY (source_object_id) REFERENCES source_objects(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_session_sources_time
        ON session_sources(session_id, session_start_ms, session_end_ms);

        CREATE TABLE IF NOT EXISTS source_integrity_audits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_object_id INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('verified', 'missing', 'mismatch', 'error')),
            expected_sha256 TEXT NOT NULL,
            actual_sha256 TEXT,
            expected_byte_size INTEGER,
            actual_byte_size INTEGER,
            details_json TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            FOREIGN KEY (source_object_id) REFERENCES source_objects(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_source_integrity_audits_source
        ON source_integrity_audits(source_object_id, id);

        CREATE TABLE IF NOT EXISTS processing_run_inputs (
            run_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            source_object_id INTEGER NOT NULL,
            source_sha256 TEXT NOT NULL,
            session_start_ms INTEGER NOT NULL,
            session_end_ms INTEGER NOT NULL,
            source_start_ms INTEGER NOT NULL,
            source_end_ms INTEGER NOT NULL,
            PRIMARY KEY (run_id, position),
            FOREIGN KEY (run_id) REFERENCES processing_runs(id) ON DELETE CASCADE,
            FOREIGN KEY (source_object_id) REFERENCES source_objects(id) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS derived_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER,
            source_object_id INTEGER,
            artifact_type TEXT NOT NULL,
            artifact_path TEXT NOT NULL,
            sha256 TEXT,
            byte_size INTEGER,
            cache_key TEXT UNIQUE,
            regenerable INTEGER NOT NULL DEFAULT 1,
            session_start_ms INTEGER,
            session_end_ms INTEGER,
            metadata_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES processing_runs(id) ON DELETE SET NULL,
            FOREIGN KEY (source_object_id) REFERENCES source_objects(id) ON DELETE SET NULL
        );
    """
