"""Published SQLite migration SQL.

Historical SQL is intentionally kept byte-for-byte equivalent to its former
location. New schema changes must be appended as a new migration.
"""

from .catalog import (
    MIGRATIONS,
    SCHEMA,
    V4_PROCESSING_RUN_COLUMNS,
    V5_GUARD_SQL,
    V5_PREDICTION_SET_COLUMNS,
    V7_GUARD_SQL,
)

__all__ = [
    "MIGRATIONS",
    "SCHEMA",
    "V4_PROCESSING_RUN_COLUMNS",
    "V5_GUARD_SQL",
    "V5_PREDICTION_SET_COLUMNS",
    "V7_GUARD_SQL",
]
