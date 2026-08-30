from __future__ import annotations

import hashlib
import sqlite3
import unittest
from pathlib import Path
from uuid import uuid4

from allday_asr.infrastructure.sqlite import (
    LATEST_SCHEMA_VERSION,
    MigrationHooks,
    MigrationRunner,
    connect_sqlite,
)
from allday_asr.infrastructure.sqlite.migrations import MIGRATIONS, SCHEMA
from allday_asr.storage.database import (
    Database,
    _backfill_v2_source_graph,
    _ensure_v4_processing_run_columns,
    _ensure_v5_prediction_set_columns,
)


EXPECTED_SCHEMA_OBJECT_COUNTS = {"index": 32, "table": 43, "trigger": 53}
EXPECTED_SCHEMA_OBJECT_SHA256 = (
    "e672177b32a040070edc27158f9f5e6752b821a822e75519372a6d6eca1fe150"
)
EXPECTED_PUBLISHED_MIGRATIONS_SHA256 = (
    "8cc7cd5f9cb5b93249fa7de3fb68641cf6060b5f9027ce1b43622db9751a9ef3"
)

TEST_ROOT = Path(__file__).parent


def cleanup_database(path: Path) -> None:
    for candidate in (path, Path(f"{path}-shm"), Path(f"{path}-wal")):
        candidate.unlink(missing_ok=True)


def migration_hooks() -> MigrationHooks:
    return MigrationHooks(
        ensure_v4_processing_run_columns=_ensure_v4_processing_run_columns,
        ensure_v5_prediction_set_columns=_ensure_v5_prediction_set_columns,
        backfill_source_graph=_backfill_v2_source_graph,
    )


class MigrationInfrastructureTests(unittest.TestCase):
    def test_constructor_does_not_create_or_migrate_database(self) -> None:
        database_path = TEST_ROOT / f"phase2c-constructor-{uuid4().hex}.sqlite3"
        try:
            database = Database(database_path)

            self.assertEqual(database.path, database_path.resolve())
            self.assertFalse(database_path.exists())

            database.initialize()

            self.assertTrue(database_path.is_file())
            self.assertEqual(database.schema_version(), LATEST_SCHEMA_VERSION)
        finally:
            cleanup_database(database_path)

    def test_open_explicitly_initializes_and_is_idempotent(self) -> None:
        database_path = TEST_ROOT / f"phase2c-open-{uuid4().hex}.sqlite3"
        try:
            first = Database.open(database_path)
            second = Database.open(database_path)

            self.assertEqual(first.schema_version(), LATEST_SCHEMA_VERSION)
            self.assertEqual(second.schema_version(), LATEST_SCHEMA_VERSION)
        finally:
            cleanup_database(database_path)

    def test_published_migration_sql_is_unchanged(self) -> None:
        payload = SCHEMA + "".join(
            f"\0{version}\0{MIGRATIONS[version]}" for version in sorted(MIGRATIONS)
        )
        self.assertEqual(
            hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            EXPECTED_PUBLISHED_MIGRATIONS_SHA256,
        )

    def test_empty_database_schema_matches_phase_2_snapshot(self) -> None:
        database_path = TEST_ROOT / f"phase2-empty-{uuid4().hex}.sqlite3"
        try:
            database = Database.open(database_path)
            self.assertEqual(database.schema_version(), LATEST_SCHEMA_VERSION)
            with database.connect() as connection:
                rows = list(
                    connection.execute(
                        """
                        SELECT type, name, tbl_name
                        FROM sqlite_master
                        WHERE name NOT LIKE 'sqlite_%'
                        ORDER BY type, name, tbl_name
                        """
                    )
                )
                self.assertEqual(
                    list(connection.execute("PRAGMA foreign_key_check")), []
                )

            counts = {
                object_type: sum(row["type"] == object_type for row in rows)
                for object_type in EXPECTED_SCHEMA_OBJECT_COUNTS
            }
            canonical = "\n".join(
                f"{row['type']}|{row['name']}|{row['tbl_name']}" for row in rows
            )
            self.assertEqual(counts, EXPECTED_SCHEMA_OBJECT_COUNTS)
            self.assertEqual(
                hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                EXPECTED_SCHEMA_OBJECT_SHA256,
            )
        finally:
            cleanup_database(database_path)

    def test_every_published_schema_version_upgrades_to_latest(self) -> None:
        token = uuid4().hex
        for historical_version in range(1, LATEST_SCHEMA_VERSION):
            with self.subTest(historical_version=historical_version):
                database_path = (
                    TEST_ROOT
                    / f"phase2-history-{token}-v{historical_version}.sqlite3"
                )
                backup_pattern = (
                    f"phase2-history-{token}-v{historical_version}.schema-v"
                    f"{historical_version}-to-v{LATEST_SCHEMA_VERSION}.*.sqlite3"
                )
                try:
                    historical_migrations = {
                        version: MIGRATIONS[version]
                        for version in range(2, historical_version + 1)
                    }
                    MigrationRunner(
                        database_path,
                        lambda path=database_path: connect_sqlite(path),
                        hooks=migration_hooks(),
                        initial_schema=SCHEMA,
                        migrations=historical_migrations,
                        latest_schema_version=historical_version,
                        now=lambda: "2026-08-30T00:00:00+00:00",
                    ).initialize()

                    database = Database.open(database_path)

                    self.assertEqual(
                        database.schema_version(), LATEST_SCHEMA_VERSION
                    )
                    backups = list(TEST_ROOT.glob(backup_pattern))
                    self.assertEqual(len(backups), 1)
                finally:
                    cleanup_database(database_path)
                    for backup in TEST_ROOT.glob(backup_pattern):
                        cleanup_database(backup)

    def test_failed_migration_rolls_back_schema_and_version_record(self) -> None:
        database_path = TEST_ROOT / f"phase2-rollback-{uuid4().hex}.sqlite3"
        try:
            runner = MigrationRunner(
                database_path,
                lambda: connect_sqlite(database_path),
                initial_schema="CREATE TABLE stable (id INTEGER PRIMARY KEY);",
                migrations={
                    2: """
                        CREATE TABLE rollback_probe (id INTEGER PRIMARY KEY);
                        INSERT INTO rollback_probe (id) VALUES (1);
                        SELECT missing_migration_function();
                    """
                },
                latest_schema_version=2,
                now=lambda: "2026-08-30T00:00:00+00:00",
            )

            with self.assertRaises(sqlite3.OperationalError):
                runner.initialize()

            connection = sqlite3.connect(database_path)
            try:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                versions = [
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]
            finally:
                connection.close()
            self.assertIn("stable", tables)
            self.assertNotIn("rollback_probe", tables)
            self.assertEqual(versions, [1])
        finally:
            cleanup_database(database_path)

    def test_higher_schema_version_is_rejected_without_mutation(self) -> None:
        database_path = TEST_ROOT / f"phase2-future-{uuid4().hex}.sqlite3"
        try:
            connection = sqlite3.connect(database_path)
            try:
                connection.executescript(
                    """
                    CREATE TABLE schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    );
                    CREATE TABLE future_only (id INTEGER PRIMARY KEY);
                    """
                )
                connection.execute(
                    "INSERT INTO schema_migrations VALUES (?, ?)",
                    (LATEST_SCHEMA_VERSION + 1, "2026-08-30T00:00:00+00:00"),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(RuntimeError, "高于程序支持"):
                Database.open(database_path)

            connection = sqlite3.connect(database_path)
            try:
                tables = {
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT name FROM sqlite_master
                        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                        """
                    )
                }
            finally:
                connection.close()
            self.assertEqual(tables, {"future_only", "schema_migrations"})
        finally:
            cleanup_database(database_path)


if __name__ == "__main__":
    unittest.main()
