from __future__ import annotations

import sqlite3
import logging
import time
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

    def daily_inventory(self, version: str, *, model: str, now):
        from .daily_inventory import DailyHistoryInventory
        return DailyHistoryInventory(self, version, model=model, now=now)

    def daily_generation_queue(self, now):
        from .daily_generation_queue import DailyGenerationQueue
        return DailyGenerationQueue(self, now)

    def daily_dirty_ranges(self, limit: int = 64) -> tuple[tuple[int, str, str], ...]:
        with self.read() as connection:
            rows = connection.execute(
                "SELECT dirty_id,first_date,last_date FROM daily_dirty_ranges "
                "ORDER BY dirty_id LIMIT ?", (limit,),
            ).fetchall()
        return tuple((int(r[0]), str(r[1]), str(r[2])) for r in rows)

    def acknowledge_daily_dirty_ranges(
        self, consumed: tuple[tuple[int, str, str | None], ...]
    ) -> None:
        with self.transaction() as connection:
            for dirty_id, expected_first, next_first in consumed:
                if next_first is None:
                    connection.execute(
                        "DELETE FROM daily_dirty_ranges WHERE dirty_id=? AND first_date=?",
                        (dirty_id, expected_first),
                    )
                else:
                    connection.execute(
                        "UPDATE daily_dirty_ranges SET first_date=? "
                        "WHERE dirty_id=? AND first_date=?",
                        (next_first, dirty_id, expected_first),
                    )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        started = time.perf_counter()
        acquired = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            acquired = time.perf_counter()
            yield connection
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            ended = time.perf_counter()
            logging.getLogger(__name__).debug(
                "sqlite transaction wait_ms=%.1f hold_ms=%.1f acquired=%s",
                ((acquired or ended) - started) * 1000,
                (ended - (acquired or ended)) * 1000,
                acquired is not None,
            )
            connection.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            yield connection
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection


__all__ = ["V3Database"]
