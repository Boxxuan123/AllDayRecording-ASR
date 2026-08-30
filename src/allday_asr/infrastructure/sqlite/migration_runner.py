from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .migrations import MIGRATIONS, SCHEMA, V5_GUARD_SQL, V7_GUARD_SQL


LATEST_SCHEMA_VERSION = max(MIGRATIONS)

ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]
ConnectionHook = Callable[[sqlite3.Connection], None]


def _noop(_connection: sqlite3.Connection) -> None:
    return None


@dataclass(frozen=True)
class MigrationHooks:
    """Data-aware compatibility hooks retained from historical migrations."""

    ensure_v4_processing_run_columns: ConnectionHook = _noop
    ensure_v5_prediction_set_columns: ConnectionHook = _noop
    backfill_source_graph: ConnectionHook = _noop


class MigrationRunner:
    """Initialize and upgrade one SQLite database through ordered migrations."""

    def __init__(
        self,
        path: Path,
        connect: ConnectionFactory,
        *,
        hooks: MigrationHooks | None = None,
        initial_schema: str = SCHEMA,
        migrations: Mapping[int, str] = MIGRATIONS,
        latest_schema_version: int | None = None,
        now: Callable[[], str] | None = None,
    ) -> None:
        self.path = path
        self.connect = connect
        self.hooks = hooks or MigrationHooks()
        self.initial_schema = initial_schema
        self.migrations = dict(migrations)
        self.latest_schema_version = (
            latest_schema_version
            if latest_schema_version is not None
            else max(self.migrations)
        )
        self.now = now or (lambda: datetime.now(timezone.utc).isoformat())

    def initialize(self) -> None:
        previous_version = self.existing_schema_version()
        if previous_version > self.latest_schema_version:
            raise RuntimeError(
                f"数据库版本 {previous_version} 高于程序支持的 "
                f"{self.latest_schema_version}"
            )
        if 0 < previous_version < self.latest_schema_version:
            self.backup_before_migration(previous_version)

        with self.connect() as connection:
            self._initialize_base_schema(connection)
            versions = {
                int(row["version"])
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            if max(versions) > self.latest_schema_version:
                raise RuntimeError(
                    f"数据库版本 {max(versions)} 高于程序支持的 "
                    f"{self.latest_schema_version}"
                )

            for version in range(2, self.latest_schema_version + 1):
                if version in versions:
                    continue
                if version == 11:
                    # V4 columns were historically added by an idempotent helper
                    # after the migration loop. V11 rebuilds processing_runs, so a
                    # database upgrading across several versions needs them first.
                    self.hooks.ensure_v4_processing_run_columns(connection)
                self._apply_migration(connection, version, self.migrations[version])

            if self.latest_schema_version >= 4:
                self.hooks.ensure_v4_processing_run_columns(connection)
            if self.latest_schema_version >= 5:
                self.hooks.ensure_v5_prediction_set_columns(connection)
                connection.executescript(V5_GUARD_SQL)
            if self.latest_schema_version >= 7:
                connection.executescript(V7_GUARD_SQL)
            if self.latest_schema_version >= 11:
                self.hooks.backfill_source_graph(connection)
            if previous_version < self.latest_schema_version:
                connection.execute("PRAGMA optimize")

    def existing_schema_version(self) -> int:
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return 0
        connection = sqlite3.connect(self.path)
        try:
            table = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'schema_migrations'
                """
            ).fetchone()
            if table is None:
                return 0
            row = connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()
            return int(row[0] or 0)
        finally:
            connection.close()

    def backup_before_migration(self, previous_version: int) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = self.path.with_name(
            f"{self.path.stem}.schema-v{previous_version}-to-v"
            f"{self.latest_schema_version}.{timestamp}{self.path.suffix}"
        )
        source = sqlite3.connect(self.path)
        destination = sqlite3.connect(backup_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        return backup_path

    def _initialize_base_schema(self, connection: sqlite3.Connection) -> None:
        schema = self._without_foreign_key_pragmas(self.initial_schema)
        applied_at = self._sql_literal(self.now())
        script = f"""
            BEGIN IMMEDIATE;
            {schema}
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations (version, applied_at)
            SELECT 1, {applied_at}
            WHERE NOT EXISTS (SELECT 1 FROM schema_migrations);
            COMMIT;
        """
        self._execute_transaction_script(connection, script)

    def _apply_migration(
        self, connection: sqlite3.Connection, version: int, sql: str
    ) -> None:
        applied_at = self._sql_literal(self.now())
        body = sql
        toggles_foreign_keys = "PRAGMA foreign_keys = OFF;" in body
        if toggles_foreign_keys:
            connection.commit()
            connection.execute("PRAGMA foreign_keys = OFF")
            body = self._without_foreign_key_pragmas(body)
            body = body.replace("BEGIN IMMEDIATE;", "", 1)
            body = body.replace("COMMIT;", "", 1)
        script = f"""
            BEGIN IMMEDIATE;
            {body}
            INSERT INTO schema_migrations (version, applied_at)
            VALUES ({version}, {applied_at});
            COMMIT;
        """
        try:
            self._execute_transaction_script(connection, script)
        finally:
            if toggles_foreign_keys:
                connection.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _execute_transaction_script(
        connection: sqlite3.Connection, script: str
    ) -> None:
        try:
            connection.executescript(script)
        except Exception:
            connection.rollback()
            raise

    @staticmethod
    def _without_foreign_key_pragmas(sql: str) -> str:
        return sql.replace("PRAGMA foreign_keys = OFF;", "").replace(
            "PRAGMA foreign_keys = ON;", ""
        )

    @staticmethod
    def _sql_literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"
