from __future__ import annotations
from datetime import datetime
from typing import Any
from allday_asr.v3.contracts import UtteranceDto, utterance_dto
from allday_asr.v3.domain.sound_kind import SOUND_KINDS, sound_uses
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

    def classify_segments(self, selections, sound_kind: str, actor: str = "desktop-user"):
        from .sound_annotation import classify_segments
        with self._uow_factory() as uow:
            return classify_segments(uow, selections, sound_kind, actor=actor, now=self._now())

    def undo_annotations(self, selections, actor="desktop-user"):
        if not isinstance(selections, list) or not 1 <= len(selections) <= 2000:
            raise ValueError("请选择需要撤销的片段")
        with self._uow_factory() as uow:
            commands = []
            seen = set()
            for selection in selections:
                uid, revision = selection["utterance_id"], selection["revision"]
                if uid in seen or type(revision) is not int:
                    raise ValueError("撤销选择无效或重复")
                seen.add(uid)
                row = uow.evidence.get_utterance(uid)
                if row.revision != revision:
                    raise UtteranceRevisionConflict("撤销前片段已变化，请重新核对")
                history = [c for c in uow.corrections.list_for_target("utterance", uid)
                           if c.before_revision == revision - 1]
                if len(history) != 1 or "previous_values" not in history[0].patch:
                    raise ValueError("此旧标注没有可验证的撤销快照，请手动纠正")
                previous = history[0].patch["previous_values"]
                commands.append(CorrectUtteranceCommand(uid, revision, previous["text"], actor,
                    speaker_track_id=previous["speaker_track_id"], change_speaker=True,
                    identity=SelfIdentity(previous["identity"]), change_identity=True,
                    sound_kind=previous["sound_kind"], person_annotation=previous["person_annotation"]))
            return {"utterances": [apply_utterance_correction(uow, c, self._now()) for c in commands]}

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
    if not text and command.sound_kind is None and not command.change_speaker:
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
    evidence = dict(before.evidence)
    if command.person_annotation is not None:
        evidence.pop("annotation_review", None)
        if command.person_annotation:
            evidence["person_annotation"] = dict(command.person_annotation)
        else:
            evidence.pop("person_annotation", None)
        patch["person_annotation"] = dict(command.person_annotation)
    if command.sound_kind is not None:
        if command.sound_kind not in SOUND_KINDS:
            raise ValueError("invalid sound kind")
        if (evidence.get("sound_kind", "speech") != command.sound_kind
            or not evidence.get("annotation_fact_ids", {}).get("sound")
            or "sound" in evidence.get("annotation_review", {}).get("dimensions", {})):
            patch["sound_kind"] = command.sound_kind
            evidence["sound_kind"] = command.sound_kind
    if not patch:
        raise ValueError("utterance correction does not change anything")
    dimensions = []
    if command.person_annotation is not None:
        dimensions.append("person")
        evidence["_annotation_track"] = selected_speaker_track_id
        evidence["_annotation_identity"] = selected_identity.value
    if "sound_kind" in patch:
        dimensions.append("sound")
    affected = []
    if dimensions:
        evidence, affected = uow.evidence.record_annotation_facts(
            before, evidence, dimensions, command.actor, now.isoformat())
    patch["previous_values"] = {"text": before.text,
        "speaker_track_id": before.speaker_track_id, "identity": before.identity.value,
        "sound_kind": before.evidence.get("sound_kind", "speech"),
        "person_annotation": before.evidence.get("person_annotation", {})}
    correction_id = new_ulid()
    updated = uow.evidence.revise_utterance(
        command.utterance_id,
        command.expected_revision,
        text,
        selected_speaker_track_id,
        selected_identity.value,
        evidence=evidence,
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
    same_task_meaning = (before.text == updated.text
        and before.identity == updated.identity
        and before.evidence.get("person_annotation", {}).get("person_id", before.speaker_track_id)
            == updated.evidence.get("person_annotation", {}).get("person_id", updated.speaker_track_id)
        and sound_uses(before.evidence)["content_usable"] == sound_uses(updated.evidence)["content_usable"])
    if not same_task_meaning:
        uow.reminders.invalidate_source_candidates(updated.utterance_id, now.isoformat())
    cascade_derivations(uow, source_type="utterance", source_id=updated.utterance_id,
        source_revision=updated.revision, reason="utterance_revision_changed",
        preserve_events=same_task_meaning, now=now)
    for uid in affected:
        if uid == updated.utterance_id:
            continue
        uow.reminders.invalidate_source_candidates(uid, now.isoformat())
        historical = uow.evidence.get_utterance(uid)
        projection = uow.evidence.annotation_projection_evidence(historical)
        if projection != historical.evidence:
            historical = uow.evidence.revise_utterance(uid, historical.revision, historical.text,
                historical.speaker_track_id, historical.identity.value, evidence=projection)
            uow.changes.append("utterance", uid, historical.revision, "upsert",
                utterance_dto(historical, speaker_label=uow.evidence.speaker_label(historical.speaker_track_id),
                    original_speaker_label=uow.evidence.speaker_label(historical.original_speaker_track_id)))
        cascade_derivations(uow, source_type="utterance", source_id=uid,
            source_revision=historical.revision, reason="human_audio_fact_superseded", now=now)
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
