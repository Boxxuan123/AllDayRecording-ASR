import sqlite3

from allday_asr.v3.adapters.sqlite.people_repository import SqlitePeopleRepository
from allday_asr.v3.adapters.sqlite.reminder_repository import SqliteReminderRepository

from .desktop_recording_queries import DesktopRecordingQueryMixin
from .desktop_review_queries import DesktopReviewQueryMixin
from .desktop_system_queries import DesktopSystemQueryMixin


class SqliteDesktopReadRepository(
    DesktopRecordingQueryMixin,
    DesktopReviewQueryMixin,
    DesktopSystemQueryMixin,
):
    """Read model for the local V3 operations UI."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection


__all__ = [
    "SqliteDesktopReadRepository",
    "SqlitePeopleRepository",
    "SqliteReminderRepository",
]
