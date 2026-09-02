from __future__ import annotations

import sqlite3

from .derivation_repository import DerivationRepositoryMixin
from .knowledge_repository_events import KnowledgeEventRepositoryMixin
from .knowledge_repository_generations import KnowledgeGenerationRepositoryMixin
from .knowledge_repository_memories import KnowledgeMemoryRepositoryMixin


class SqliteKnowledgeRepository(
    KnowledgeGenerationRepositoryMixin,
    KnowledgeEventRepositoryMixin,
    KnowledgeMemoryRepositoryMixin,
):
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection


class SqliteDerivationRepository(DerivationRepositoryMixin):
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
