from __future__ import annotations

import shutil
import json
import sqlite3
import unittest
from pathlib import Path
from uuid import uuid4

from allday_asr.v3.adapters.sqlite import (
    LATEST_V3_SCHEMA_VERSION,
    V3Database,
    V3Migration,
    V3MigrationRunner,
)
from allday_asr.v3.bootstrap import V3CorePaths, compose_v3_core
from allday_asr.v3.adapters.sqlite.migrations import MIGRATIONS


TEST_ROOT = Path(__file__).parent


class V3MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TEST_ROOT / f"v3b-migration-{uuid4().hex}"

    def tearDown(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_composition_and_database_constructor_have_no_io(self) -> None:
        paths = V3CorePaths.from_state_dir(self.directory)

        core = compose_v3_core(paths)

        self.assertEqual(core.database.path, paths.database_path)
        self.assertFalse(self.directory.exists())

    def test_empty_database_repeatedly_migrates_to_latest_wal_schema(self) -> None:
        path = self.directory / "core.sqlite3"
        database = V3Database(path)

        self.assertEqual(database.initialize(), LATEST_V3_SCHEMA_VERSION)
        self.assertEqual(database.initialize(), LATEST_V3_SCHEMA_VERSION)
        self.assertEqual(database.schema_version(), LATEST_V3_SCHEMA_VERSION)

        with database.read() as connection:
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            foreign_key_errors = list(connection.execute("PRAGMA foreign_key_check"))
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertEqual(journal_mode, "wal")
        self.assertEqual(foreign_key_errors, [])
        self.assertIn("recording_sessions", tables)
        self.assertNotIn("source_objects", tables)

    def test_failed_migration_rolls_back_its_schema_and_version(self) -> None:
        path = self.directory / "core.sqlite3"
        runner = V3MigrationRunner(
            path,
            migrations=(
                V3Migration(
                    1,
                    "base",
                    """
                    CREATE TABLE schema_migrations (
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        applied_at TEXT NOT NULL
                    );
                    CREATE TABLE stable (id INTEGER PRIMARY KEY);
                    """,
                ),
                V3Migration(
                    2,
                    "broken",
                    """
                    CREATE TABLE half_written (id INTEGER PRIMARY KEY);
                    INSERT INTO half_written (id) VALUES (1);
                    SELECT missing_v3_migration_function();
                    """,
                ),
            ),
            now=lambda: "2026-08-31T00:00:00Z",
        )

        with self.assertRaises(sqlite3.OperationalError):
            runner.initialize()

        connection = sqlite3.connect(path)
        try:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            versions = list(
                connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            )
        finally:
            connection.close()
        self.assertIn("stable", tables)
        self.assertNotIn("half_written", tables)
        self.assertEqual(versions, [(1,)])

    def test_device_sync_migration_backfills_rebuildable_projection_payloads(
        self,
    ) -> None:
        path = self.directory / "core.sqlite3"
        V3MigrationRunner(path, migrations=(MIGRATIONS[0],)).initialize()
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                """
                INSERT INTO recording_sessions (
                    session_id, captured_start, captured_end, timezone, state,
                    revision, status_code, current_stage, progress,
                    blocking_reason, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "session-1",
                    "2026-08-31T00:00:00.000000Z",
                    "2026-08-31T00:01:00.000000Z",
                    "Asia/Singapore",
                    "ready_for_processing",
                    1,
                    "ready",
                    None,
                    1.0,
                    None,
                    "2026-08-31T00:00:00.000000Z",
                    "2026-08-31T00:00:00.000000Z",
                ),
            )
            connection.execute(
                """
                INSERT INTO change_events (
                    resource_type, resource_id, revision, operation,
                    payload_json, created_at
                ) VALUES ('recording_session', 'session-1', 1, 'upsert', NULL, ?)
                """,
                ("2026-08-31T00:00:00.000000Z",),
            )
            connection.commit()
        finally:
            connection.close()

        V3Database(path).initialize()

        with sqlite3.connect(path) as migrated:
            payload = json.loads(
                migrated.execute(
                    "SELECT payload_json FROM change_events"
                ).fetchone()[0]
            )
        self.assertEqual(payload["session_id"], "session-1")
        self.assertEqual(payload["status_code"], "ready")

    def test_transaction_rolls_back_all_writes_on_error(self) -> None:
        database = V3Database.open(self.directory / "core.sqlite3")

        with self.assertRaisesRegex(RuntimeError, "abort"):
            with database.transaction() as connection:
                connection.execute(
                    "INSERT INTO tombstones VALUES (?, ?, ?, ?, ?)",
                    ("session", "01TEST", 1, None, "2026-08-31T00:00:00Z"),
                )
                raise RuntimeError("abort")

        with database.read() as connection:
            count = connection.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
