from __future__ import annotations

import sqlite3
from types import TracebackType

from allday_asr.v3.adapters.sqlite.database import V3Database
from allday_asr.v3.adapters.sqlite.processing_repositories import (
    SqliteAdmissionRepository,
    SqliteDurableProcessingRepository,
    SqliteEvidenceProjectionRepository,
)
from allday_asr.v3.adapters.sqlite.desktop_repository import (
    SqliteDesktopReadRepository,
)
from allday_asr.v3.adapters.sqlite.knowledge_repositories import (
    SqliteDerivationRepository,
    SqliteKnowledgeRepository,
)
from allday_asr.v3.adapters.sqlite.reminder_repository import (
    SqliteReminderRepository,
)
from allday_asr.v3.adapters.sqlite.people_repository import SqlitePeopleRepository
from allday_asr.v3.adapters.sqlite.person_memory_repository import (
    SqlitePersonMemoryRepository,
)
from allday_asr.v3.adapters.sqlite.insight_repository import SqliteInsightRepository
from allday_asr.v3.adapters.sqlite.repositories import (
    Clock,
    SqliteArtifactRepository,
    SqliteAuditRepository,
    SqliteChangeLogRepository,
    SqliteCorrectionRepository,
    SqliteDeviceRepository,
    SqliteDeviceTrustRepository,
    SqliteIdempotencyRepository,
    SqliteLegacyImportRunRepository,
    SqliteMobileSyncRepository,
    SqliteProcessingRunRepository,
    SqliteRecordingCatalogRepository,
    SqliteTombstoneRepository,
    utc_now,
)


class SqliteUnitOfWork:
    def __init__(self, database: V3Database, *, now: Clock = utc_now) -> None:
        self.database = database
        self.now = now
        self._context = None
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> SqliteUnitOfWork:
        if self._connection is not None:
            raise RuntimeError("unit of work is already active")
        self._context = self.database.transaction()
        self._connection = self._context.__enter__()
        connection = self._connection
        self.catalog = SqliteRecordingCatalogRepository(connection)
        self.devices = SqliteDeviceRepository(connection)
        self.device_trust = SqliteDeviceTrustRepository(connection, now=self.now)
        self.mobile_sync = SqliteMobileSyncRepository(connection, now=self.now)
        self.processing_runs = SqliteProcessingRunRepository(connection)
        self.processing = SqliteDurableProcessingRepository(connection, now=self.now)
        self.admission = SqliteAdmissionRepository(connection, now=self.now)
        self.artifacts = SqliteArtifactRepository(connection)
        self.evidence = SqliteEvidenceProjectionRepository(connection, now=self.now)
        self.corrections = SqliteCorrectionRepository(connection)
        self.changes = SqliteChangeLogRepository(connection, now=self.now)
        self.audit = SqliteAuditRepository(connection, now=self.now)
        self.idempotency = SqliteIdempotencyRepository(connection, now=self.now)
        self.tombstones = SqliteTombstoneRepository(connection, now=self.now)
        self.legacy_imports = SqliteLegacyImportRunRepository(
            connection, now=self.now
        )
        self.desktop = SqliteDesktopReadRepository(connection)
        self.knowledge = SqliteKnowledgeRepository(connection)
        self.derivations = SqliteDerivationRepository(connection)
        self.reminders = SqliteReminderRepository(connection)
        self.people = SqlitePeopleRepository(connection)
        self.person_memories = SqlitePersonMemoryRepository(connection)
        self.insights = SqliteInsightRepository(connection)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        if self._context is None:
            return None
        try:
            return self._context.__exit__(exc_type, exc_value, traceback)
        finally:
            self._connection = None
            self._context = None


__all__ = ["SqliteUnitOfWork"]
