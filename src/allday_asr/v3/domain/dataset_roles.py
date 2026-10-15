"""Reservation is a session-level commitment made before any identity is known."""

from enum import StrEnum


class DatasetRole(StrEnum):
    LEARNING = "learning"
    BLIND = "blind"
    HOLDOUT = "holdout"


class DatasetIsolationError(ValueError):
    pass


def require_learning(role):
    if not is_learning(role):
        raise DatasetIsolationError("dataset_role_excluded: enrollment requires a learning session")


def is_learning(role):
    return role == DatasetRole.LEARNING
