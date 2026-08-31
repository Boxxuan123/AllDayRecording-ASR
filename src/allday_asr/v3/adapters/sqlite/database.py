from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from allday_asr.v3.adapters.sqlite.migration_runner import V3MigrationRunner


class V3Database:
    """Explicit lifecycle for the independent V3 Core SQLite database."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.migrations = V3MigrationRunner(self.path)

    @classmethod
    def open(cls, path: Path) -> V3Database:
        database = cls(path)
        database.initialize()
        return database

    def initialize(self) -> int:
        return self.migrations.initialize()

    def schema_version(self) -> int:
        return self.migrations.schema_version()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection


__all__ = ["V3Database"]
