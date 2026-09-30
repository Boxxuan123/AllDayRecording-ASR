"""Reservation is a session-level commitment made before any identity is known."""

from enum import StrEnum


class DatasetRole(StrEnum):
    LEARNING = "learning"
    BLIND = "blind"
    HOLDOUT = "holdout"


class DatasetIsolationError(ValueError):
    pass


def require_learning(connection, session_id):
    row = connection.execute("SELECT dataset_role FROM session_dataset_roles WHERE session_id=?", (session_id,)).fetchone()
    if row is None or row[0] != DatasetRole.LEARNING:
        raise DatasetIsolationError("dataset_role_excluded: enrollment requires a learning session")


def is_learning(connection, session_id):
    row = connection.execute("SELECT dataset_role FROM session_dataset_roles WHERE session_id=?", (session_id,)).fetchone()
    return row is not None and row[0] == DatasetRole.LEARNING
