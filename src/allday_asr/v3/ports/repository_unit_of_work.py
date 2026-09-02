from __future__ import annotations
from types import TracebackType
from typing import Protocol, Self

from .repository_ports_recording import (
    RecordingCatalogRepository,
    DeviceRepository,
    ProcessingRunRepository,
    ArtifactRepository,
)
from .repository_ports_processing import (
    DurableProcessingRepository,
    AdmissionRepository,
    EvidenceProjectionRepository,
    CorrectionRepository,
)
from .repository_ports_sync import (
    ChangeLogRepository,
    DeviceTrustRepository,
    MobileSyncRepository,
    AuditRepository,
    IdempotencyRepository,
    TombstoneRepository,
    DesktopReadRepository,
)
from .repository_ports_knowledge import (
    KnowledgeRepository,
    DerivationRepository,
    ReminderRepository,
)
from .repository_ports_people import (
    PeopleRepository,
    PersonMemoryRepository,
    InsightRepository,
)


class UnitOfWork(Protocol):
    catalog: RecordingCatalogRepository
    devices: DeviceRepository
    processing_runs: ProcessingRunRepository
    processing: DurableProcessingRepository
    admission: AdmissionRepository
    artifacts: ArtifactRepository
    evidence: EvidenceProjectionRepository
    corrections: CorrectionRepository
    changes: ChangeLogRepository
    device_trust: DeviceTrustRepository
    mobile_sync: MobileSyncRepository
    audit: AuditRepository
    idempotency: IdempotencyRepository
    tombstones: TombstoneRepository
    desktop: DesktopReadRepository
    knowledge: KnowledgeRepository
    derivations: DerivationRepository
    reminders: ReminderRepository
    people: PeopleRepository
    person_memories: PersonMemoryRepository
    insights: InsightRepository

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...
