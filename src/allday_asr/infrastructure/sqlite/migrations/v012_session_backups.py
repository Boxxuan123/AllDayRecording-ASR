"""Published schema migration version 12."""

SQL = """
        CREATE TABLE session_backups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            backup_key TEXT NOT NULL UNIQUE,
            session_id INTEGER NOT NULL,
            storage_kind TEXT NOT NULL CHECK(
                storage_kind IN ('independent_device', 'network', 'same_device_test')
            ),
            backup_path TEXT NOT NULL UNIQUE,
            input_fingerprint TEXT NOT NULL,
            backup_manifest_sha256 TEXT NOT NULL,
            file_count INTEGER NOT NULL,
            total_bytes INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('verified', 'failed')),
            created_at TEXT NOT NULL,
            verified_at TEXT,
            restore_verified_at TEXT,
            last_checked_at TEXT,
            error TEXT,
            UNIQUE(session_id, backup_path, input_fingerprint),
            CHECK(file_count > 0),
            CHECK(total_bytes >= 0),
            FOREIGN KEY (session_id) REFERENCES recording_sessions(id) ON DELETE RESTRICT
        );

        CREATE INDEX idx_session_backups_session_status
        ON session_backups(session_id, status, id);

        CREATE TABLE session_backup_files (
            backup_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            file_kind TEXT NOT NULL CHECK(
                file_kind IN ('source_audio', 'capture_manifest')
            ),
            source_instance_id INTEGER,
            relative_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            byte_size INTEGER NOT NULL,
            PRIMARY KEY (backup_id, position),
            UNIQUE(backup_id, relative_path),
            CHECK(byte_size >= 0),
            CHECK(
                (file_kind = 'source_audio' AND source_instance_id IS NOT NULL)
                OR (file_kind = 'capture_manifest' AND source_instance_id IS NULL)
            ),
            FOREIGN KEY (backup_id) REFERENCES session_backups(id) ON DELETE RESTRICT,
            FOREIGN KEY (source_instance_id) REFERENCES source_instances(id) ON DELETE RESTRICT
        );

        CREATE TRIGGER protect_session_backups_immutable_fields
        BEFORE UPDATE OF
            backup_key, session_id, storage_kind, backup_path,
            input_fingerprint, backup_manifest_sha256, file_count,
            total_bytes, created_at
        ON session_backups
        BEGIN
            SELECT RAISE(ABORT, 'immutable session backup metadata cannot be changed');
        END;

        CREATE TRIGGER protect_session_backups_from_delete
        BEFORE DELETE ON session_backups
        BEGIN
            SELECT RAISE(ABORT, 'session backup evidence cannot be deleted');
        END;

        CREATE TRIGGER protect_session_backup_files_from_update
        BEFORE UPDATE ON session_backup_files
        BEGIN
            SELECT RAISE(ABORT, 'session backup file evidence cannot be changed');
        END;

        CREATE TRIGGER protect_session_backup_files_from_delete
        BEFORE DELETE ON session_backup_files
        BEGIN
            SELECT RAISE(ABORT, 'session backup file evidence cannot be deleted');
        END;
    """
