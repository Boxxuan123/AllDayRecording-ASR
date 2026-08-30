"""SQLite connection and schema migration infrastructure."""

from .connection import connect_sqlite
from .migration_runner import LATEST_SCHEMA_VERSION, MigrationHooks, MigrationRunner

__all__ = [
    "LATEST_SCHEMA_VERSION",
    "MigrationHooks",
    "MigrationRunner",
    "connect_sqlite",
]
