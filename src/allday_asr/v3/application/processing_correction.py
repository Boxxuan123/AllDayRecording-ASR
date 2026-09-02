from __future__ import annotations
from datetime import datetime
from typing import Any
from allday_asr.v3.contracts import UtteranceDto, utterance_dto
from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.identity import SelfIdentity
from allday_asr.v3.domain.models import (
    ChangeOperation,
    CorrectionOperation,
)
from allday_asr.v3.domain.processing import (
    ArtifactInvalidation,
)
from allday_asr.v3.ports.repositories import UnitOfWork
from allday_asr.v3.application.knowledge import cascade_derivations

from .durable_processing_types import (
    CorrectUtteranceCommand,
    DateTimeClock,
    UnitOfWorkFactory,
    UtteranceRevisionConflict,
)
from .durable_processing_support import (
    _utc_now,
)


class CorrectionInvalidationService:
    def __init__(
        self, uow_factory: UnitOfWorkFactory, *, now: DateTimeClock | None = None
    ) -> None:
        self._uow_factory = uow_factory
        self._now = now or _utc_now

    def correct_utterance(self, command: CorrectUtteranceCommand) -> UtteranceDto:
        now = self._now()
        with self._uow_factory() as uow:
            return apply_utterance_correction(uow, command, now)

    def correction_history(self, utterance_id: str) -> dict[str, Any]:
        with self._uow_factory() as uow:
            utterance = uow.evidence.get_utterance(utterance_id)
            current_label = uow.evidence.speaker_label(utterance.speaker_track_id)
            original_label = uow.evidence.speaker_label(
                utterance.original_speaker_track_id
            )
            operations = uow.corrections.list_for_target("utterance", utterance_id)
            return {
                "utterance": utterance_dto(
                    utterance,
                    speaker_label=current_label,
                    original_speaker_label=original_label,
                ),
                "operations": [
                    {
                        "correction_id": operation.correction_id,
                        "before_revision": operation.before_revision,
                        "patch": operation.patch,
                        "actor": operation.actor,
                        "created_at": operation.created_at.isoformat(),
                    }
                    for operation in operations
                ],
            }


def apply_utterance_correction(
    uow: UnitOfWork,
    command: CorrectUtteranceCommand,
    now: datetime,
) -> UtteranceDto:
    text = command.text.strip()
    if not text:
        raise ValueError("utterance text cannot be empty")
    before = uow.evidence.get_utterance(command.utterance_id)
    if before.revision != command.expected_revision:
        raise UtteranceRevisionConflict("utterance revision conflict")
    patch: dict[str, Any] = {}
    if before.text != text:
        patch["text"] = text
    selected_speaker_track_id = (
        command.speaker_track_id if command.change_speaker else before.speaker_track_id
    )
    if before.speaker_track_id != selected_speaker_track_id:
        patch["speaker_track_id"] = selected_speaker_track_id
    if command.change_identity and command.identity is None:
        raise ValueError("utterance identity correction is missing")
    selected_identity = (
        SelfIdentity(command.identity) if command.change_identity else before.identity
    )
    if before.identity != selected_identity:
        patch["identity"] = selected_identity.value
    if not patch:
        raise ValueError("utterance correction does not change anything")
    correction_id = new_ulid()
    updated = uow.evidence.revise_utterance(
        command.utterance_id,
        command.expected_revision,
        text,
        selected_speaker_track_id,
        selected_identity.value,
    )
    correction_added = uow.corrections.add(
        CorrectionOperation(
            correction_id=correction_id,
            target_type="utterance",
            target_id=command.utterance_id,
            before_revision=before.revision,
            patch=patch,
            actor=command.actor,
            created_at=now,
        )
    )
    if not correction_added:
        raise RuntimeError("utterance correction operation already exists")
    speaker_label = uow.evidence.speaker_label(updated.speaker_track_id)
    original_speaker_label = uow.evidence.speaker_label(
        updated.original_speaker_track_id
    )
    for artifact_id in uow.artifacts.dependent_ids("utterance", command.utterance_id):
        uow.artifacts.invalidate(
            ArtifactInvalidation(
                status_event_id=new_ulid(),
                artifact_id=artifact_id,
                status="stale",
                reason="utterance_revision_changed",
                source_type="utterance",
                source_id=updated.utterance_id,
                source_revision=updated.revision,
                created_at=now,
            )
        )
    cascade_derivations(
        uow,
        source_type="utterance",
        source_id=updated.utterance_id,
        source_revision=updated.revision,
        reason="utterance_revision_changed",
        now=now,
    )
    dto = utterance_dto(
        updated,
        speaker_label=speaker_label,
        original_speaker_label=original_speaker_label,
    )
    uow.changes.append(
        "utterance",
        updated.utterance_id,
        updated.revision,
        ChangeOperation.UPSERT.value,
        dto,
    )
    uow.audit.append(
        "utterance.corrected",
        command.actor,
        "utterance",
        updated.utterance_id,
        {
            "revision": updated.revision,
            "correction_id": correction_id,
            "fields": sorted(patch),
        },
    )
    return dto
