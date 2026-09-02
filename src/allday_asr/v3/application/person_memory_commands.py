from __future__ import annotations

from datetime import datetime
from typing import Any

from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.person_memory import (
    PersonMemoryConfirmation,
    PersonMemoryDraft,
    PersonMemoryKind,
    PersonMemoryOperationKind,
    PersonMemorySource,
    PersonMemoryStatus,
)

from .person_memory_support import (
    _datetime,
    _optional_datetime,
    _parse_datetime,
)


class PersonMemoryCommandMixin:
    def create(
        self,
        draft: PersonMemoryDraft,
        *,
        actor: str = "desktop-user",
    ) -> dict[str, Any]:
        source = (
            PersonMemorySource.MODEL
            if draft.kind is PersonMemoryKind.MODEL_OBSERVATION
            else PersonMemorySource.HUMAN
        )
        confirmation = (
            PersonMemoryConfirmation.INFERRED
            if source is PersonMemorySource.MODEL
            else draft.confirmation
        )
        with self._uow_factory() as uow:
            return uow.person_memories.add_revision(
                memory_id=new_ulid(),
                person_id=draft.person_id,
                kind=draft.kind.value,
                summary=draft.summary,
                details=draft.details,
                source=source.value,
                confidence=draft.confidence,
                confirmation_status=confirmation.value,
                valid_from=_datetime(draft.valid_from),
                valid_until=(
                    _datetime(draft.valid_until)
                    if draft.valid_until is not None
                    else None
                ),
                status=PersonMemoryStatus.ACTIVE.value,
                event_id=draft.event_id,
                reminder_event_id=draft.reminder_event_id,
                evidence_utterance_ids=draft.evidence_utterance_ids,
                operation_id=new_ulid(),
                operation_kind=PersonMemoryOperationKind.CREATE.value,
                actor=actor,
                created_at=_datetime(self._now()),
                operation_payload={"source": source.value},
            )
    def revise(
        self,
        memory_id: str,
        *,
        summary: str,
        details: dict[str, Any],
        confidence: float,
        valid_from: datetime,
        valid_until: datetime | None,
        reminder_event_id: str | None,
        actor: str = "desktop-user",
    ) -> dict[str, Any]:
        with self._uow_factory() as uow:
            current = uow.person_memories.current_memory(memory_id)
        evidence_ids = tuple(
            str(item["utterance_id"])
            for item in current["evidence"]
            if item.get("utterance_id") is not None
        )
        draft = PersonMemoryDraft(
            person_id=str(current["person_id"]),
            kind=PersonMemoryKind(str(current["kind"])),
            summary=summary,
            details=details,
            confidence=confidence,
            confirmation=PersonMemoryConfirmation.CONFIRMED,
            valid_from=valid_from,
            valid_until=valid_until,
            event_id=current["event_id"],
            reminder_event_id=reminder_event_id,
            evidence_utterance_ids=evidence_ids,
        )
        with self._uow_factory() as uow:
            return uow.person_memories.add_revision(
                memory_id=memory_id,
                person_id=draft.person_id,
                kind=draft.kind.value,
                summary=draft.summary,
                details=draft.details,
                source=PersonMemorySource.HUMAN.value,
                confidence=draft.confidence,
                confirmation_status=PersonMemoryConfirmation.CONFIRMED.value,
                valid_from=_datetime(draft.valid_from),
                valid_until=(
                    _datetime(draft.valid_until)
                    if draft.valid_until is not None
                    else None
                ),
                status=PersonMemoryStatus.ACTIVE.value,
                event_id=draft.event_id,
                reminder_event_id=draft.reminder_event_id,
                evidence_utterance_ids=draft.evidence_utterance_ids,
                operation_id=new_ulid(),
                operation_kind=PersonMemoryOperationKind.REVISE.value,
                actor=actor,
                created_at=_datetime(self._now()),
                operation_payload={"previous_revision": current["revision"]},
            )
    def confirm(
        self, memory_id: str, *, actor: str = "desktop-user"
    ) -> dict[str, Any]:
        """Confirm the current value without making the user maintain a copy."""
        with self._uow_factory() as uow:
            current = uow.person_memories.current_memory(memory_id)
        return self.revise(
            memory_id,
            summary=str(current["summary"]),
            details=dict(current["details"]),
            confidence=float(current["confidence"]),
            valid_from=_parse_datetime(current["valid_from"]),
            valid_until=_optional_datetime(current.get("valid_until")),
            reminder_event_id=current.get("reminder_event_id"),
            actor=actor,
        )
    def expire(self, memory_id: str, actor: str = "desktop-user") -> dict[str, Any]:
        return self._status(
            memory_id,
            PersonMemoryStatus.EXPIRED,
            PersonMemoryOperationKind.EXPIRE,
            actor,
        )
    def retract(self, memory_id: str, actor: str = "desktop-user") -> dict[str, Any]:
        return self._status(
            memory_id,
            PersonMemoryStatus.RETRACTED,
            PersonMemoryOperationKind.RETRACT,
            actor,
        )
    def undo(self, memory_id: str, actor: str = "desktop-user") -> dict[str, Any]:
        with self._uow_factory() as uow:
            return uow.person_memories.undo(
                memory_id, actor, new_ulid(), _datetime(self._now())
            )
    def _status(
        self,
        memory_id: str,
        status: PersonMemoryStatus,
        operation: PersonMemoryOperationKind,
        actor: str,
    ) -> dict[str, Any]:
        with self._uow_factory() as uow:
            return uow.person_memories.revise_status(
                memory_id,
                status.value,
                actor,
                new_ulid(),
                operation.value,
                _datetime(self._now()),
            )
