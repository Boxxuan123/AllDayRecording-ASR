from __future__ import annotations

import sqlite3

from .person_memory_repository_queries import PersonMemoryQueryRepositoryMixin
from .person_memory_repository_records import PersonMemoryRecordRepositoryMixin
from .person_memory_repository_support import PersonMemoryRepositorySupportMixin


class SqlitePersonMemoryRepository(
    PersonMemoryRecordRepositoryMixin,
    PersonMemoryQueryRepositoryMixin,
    PersonMemoryRepositorySupportMixin,
):
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
