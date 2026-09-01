from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class V3Migration:
    version: int
    name: str
    sql: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def _default_migrations() -> tuple[V3Migration, ...]:
    from allday_asr.v3.adapters.sqlite.migrations import MIGRATIONS

    return MIGRATIONS


LATEST_V3_SCHEMA_VERSION = 8


class V3MigrationRunner:
    def __init__(
        self,
        path: Path,
        *,
        migrations: Iterable[V3Migration] | None = None,
        now: Callable[[], str] | None = None,
    ) -> None:
        self.path = path.resolve()
        selected = tuple(migrations if migrations is not None else _default_migrations())
        self.migrations = tuple(sorted(selected, key=lambda migration: migration.version))
        if not self.migrations:
            raise ValueError("at least one V3 migration is required")
        versions = [migration.version for migration in self.migrations]
        if versions != list(range(1, max(versions) + 1)):
            raise ValueError("V3 migrations must be contiguous and start at version 1")
        self.latest_version = max(versions)
        self.now = now or _utc_now

    def initialize(self) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = _connect(self.path)
        try:
            applied = self._applied(connection)
            if applied and max(applied) > self.latest_version:
                raise RuntimeError(
                    f"V3 database schema {max(applied)} is newer than supported "
                    f"version {self.latest_version}"
                )
            for migration in self.migrations:
                recorded = applied.get(migration.version)
                if recorded is not None:
                    if recorded != (migration.name, migration.sha256):
                        raise RuntimeError(
                            f"V3 migration {migration.version} checksum/name mismatch"
                        )
                    continue
                self._apply(connection, migration)
            return self.latest_version
        finally:
            connection.close()

    def schema_version(self) -> int:
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return 0
        connection = sqlite3.connect(self.path)
        try:
            row = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='schema_migrations'"
            ).fetchone()
            if row is None:
                return 0
            version = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()
            return int(version[0])
        finally:
            connection.close()

    @staticmethod
    def _applied(connection: sqlite3.Connection) -> dict[int, tuple[str, str]]:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='schema_migrations'"
        ).fetchone()
        if exists is None:
            return {}
        return {
            int(row["version"]): (str(row["name"]), str(row["sha256"]))
            for row in connection.execute(
                "SELECT version, name, sha256 FROM schema_migrations"
            )
        }

    def _apply(self, connection: sqlite3.Connection, migration: V3Migration) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in _statements(migration.sql):
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations "
                "(version, name, sha256, applied_at) VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.sha256, self.now()),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


def _statements(sql: str) -> tuple[str, ...]:
    statements: list[str] = []
    pending = ""
    for line in sql.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            if statement:
                statements.append(statement)
            pending = ""
    if pending.strip():
        raise ValueError("incomplete SQL statement in V3 migration")
    return tuple(statements)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = ["LATEST_V3_SCHEMA_VERSION", "V3Migration", "V3MigrationRunner"]
