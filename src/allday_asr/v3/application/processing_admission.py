from __future__ import annotations
from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.models import (
    ChangeOperation,
)
from allday_asr.v3.domain.processing import (
    BackupEvidence,
    BackupEvidenceStatus,
)

from .durable_processing_types import (
    DateTimeClock,
    RecordBackupEvidenceCommand,
    UnitOfWorkFactory,
)
from .durable_processing_support import (
    _session_projection,
    _utc_now,
)


class AdmissionService:
    def __init__(
        self, uow_factory: UnitOfWorkFactory, *, now: DateTimeClock | None = None
    ) -> None:
        self._uow_factory = uow_factory
        self._now = now or _utc_now

    def record_verified_backup(self, command: RecordBackupEvidenceCommand) -> bool:
        now = self._now()
        evidence = BackupEvidence(
            evidence_id=new_ulid(),
            session_id=command.session_id,
            provider=command.provider,
            storage_kind=command.storage_kind,
            digest=command.digest.lower(),
            status=BackupEvidenceStatus.VERIFIED,
            restore_checked_at=command.restore_checked_at,
            metadata=command.metadata,
            created_at=now,
        )
        with self._uow_factory() as uow:
            added = uow.admission.add_evidence(evidence)
            admitted, reason = uow.admission.evaluate(command.session_id)
            revision = uow.admission.apply(command.session_id, admitted, reason)
            session = uow.catalog.get_session(command.session_id)
            uow.changes.append(
                "recording_session",
                session.session_id,
                revision,
                ChangeOperation.UPSERT.value,
                _session_projection(session),
            )
            uow.audit.append(
                "session.admission.evaluated",
                "system",
                "recording_session",
                command.session_id,
                {
                    "admitted": admitted,
                    "blocking_reason": reason,
                    "backup_evidence_id": evidence.evidence_id,
                },
            )
        return added and admitted

    def evaluate(self, session_id: str) -> bool:
        with self._uow_factory() as uow:
            admitted, reason = uow.admission.evaluate(session_id)
            revision = uow.admission.apply(session_id, admitted, reason)
            session = uow.catalog.get_session(session_id)
            uow.changes.append(
                "recording_session",
                session_id,
                revision,
                ChangeOperation.UPSERT.value,
                _session_projection(session),
            )
        return admitted
