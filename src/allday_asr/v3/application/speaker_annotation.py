from __future__ import annotations
from datetime import datetime
from typing import Any
from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.identity import SelfIdentity
from allday_asr.v3.domain.processing import SpeakerTrack
from allday_asr.v3.ports.repositories import UnitOfWork
from .durable_processing import CorrectUtteranceCommand, UtteranceRevisionConflict, apply_utterance_correction
from .people_support import _datetime


class AnnotationValidationError(ValueError):
    pass


def assign_annotation(uow: UnitOfWork, selections: list[dict[str, Any]], *,
                      person_id: str | None, display_name: str | None, actor: str,
                      now: datetime, new_person_id: str | None = None) -> dict[str, Any]:
    if not selections or len(selections) > 2000:
        raise AnnotationValidationError("请选择 1 至 2000 条发言")
    if bool(person_id) == bool(display_name):
        raise AnnotationValidationError("请选择已有的人物或输入新人物姓名")
    utterances = []
    seen = set()
    for selection in selections:
        if not isinstance(selection, dict):
            raise AnnotationValidationError("发言选择无效")
        utterance_id = selection.get("utterance_id")
        revision = selection.get("revision")
        if not isinstance(utterance_id, str) or type(revision) is not int or utterance_id in seen:
            raise AnnotationValidationError("发言选择无效或重复")
        seen.add(utterance_id)
        try:
            utterance = uow.evidence.get_utterance(utterance_id)
        except KeyError as exc:
            raise AnnotationValidationError("发言已不存在") from exc
        if utterance.revision != revision or utterance.status != "active":
            raise UtteranceRevisionConflict("发言已变化，请刷新并在审核中核对后重新提交")
        if not uow.evidence.annotation_is_current(utterance, "person"):
            raise UtteranceRevisionConflict("human fact superseded; refresh before correcting")
        utterances.append(utterance)
    if person_id:
        person = next((p for p in uow.people.list_people() if p["person_id"] == person_id), None)
        if person is None or person["kind"] == "unknown":
            raise AnnotationValidationError("人物已不存在或不可标注")
        name, kind = str(person["display_name"]), str(person["kind"])
    else:
        name = (display_name or "").strip()
        if not name or len(name) > 100:
            raise AnnotationValidationError("人物姓名应为 1 至 100 字")
        person_id, kind = new_person_id or new_ulid(), "known"
        existing = next((p for p in uow.people.list_people() if p["person_id"] == person_id), None)
        if existing is None:
            uow.people.create_person(person_id, name, kind, _datetime(now))
        elif existing["display_name"] != name or existing["kind"] != kind:
            raise AnnotationValidationError("本地人物 ID 已被其他人物使用")
    tracks = {}
    for utterance in utterances:
        if utterance.session_id not in tracks:
            track_id = new_ulid()
            created = uow.evidence.add_speaker_track(SpeakerTrack(
                track_id, utterance.session_id, utterance.run_id, f"manual:{track_id}",
                utterance.source_artifact_id, now))
            if not created:
                raise RuntimeError("manual speaker track was not created")
            tracks[utterance.session_id] = track_id
    cluster_id = new_ulid()
    uow.people.add_manual_group(cluster_id, tuple(tracks.values()), name, actor,
                                new_ulid(), _datetime(now))
    uow.people.label_cluster(cluster_id, person_id, actor, new_ulid(), _datetime(now))
    identity = SelfIdentity.SELF if kind == "self" else SelfIdentity.NOT_SELF
    updated = []
    for utterance in utterances:
        updated.append(apply_utterance_correction(uow, CorrectUtteranceCommand(
            utterance_id=utterance.utterance_id, expected_revision=utterance.revision,
            text=utterance.text, actor=actor, speaker_track_id=tracks[utterance.session_id],
            change_speaker=True, identity=identity, change_identity=True,
            person_annotation={"person_id": person_id, "source": "human", "actor": actor,
                "confirmed_at": now.isoformat(), "source_revision": utterance.revision,
                "source_utterance_id": utterance.utterance_id,
                "session_id": utterance.session_id, "start_ms": utterance.start_ms,
                "end_ms": utterance.end_ms, "audio": uow.evidence.audio_evidence(utterance)}), now))
    uow.audit.append("speaker.selection_assigned", actor, "person", person_id,
                     {"utterance_ids": sorted(seen), "cluster_id": cluster_id})
    return {"person_id": person_id, "cluster_id": cluster_id, "utterances": updated}
