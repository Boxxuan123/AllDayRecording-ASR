from __future__ import annotations
from datetime import datetime
from allday_asr.v3.domain.sound_kind import SOUND_KINDS
from allday_asr.v3.contracts import utterance_dto
from .durable_processing import CorrectUtteranceCommand, UtteranceRevisionConflict, apply_utterance_correction
from .speaker_annotation import AnnotationValidationError


def classify_segments(uow, selections, sound_kind: str, *, actor: str, now: datetime):
    if not isinstance(sound_kind, str) or sound_kind not in SOUND_KINDS:
        raise AnnotationValidationError("片段类型无效")
    if not isinstance(selections, list) or not 1 <= len(selections) <= 2000:
        raise AnnotationValidationError("请选择 1 至 2000 个片段")
    rows, seen = [], set()
    for selection in selections:
        if not isinstance(selection, dict):
            raise AnnotationValidationError("片段选择无效")
        uid, revision = selection.get("utterance_id"), selection.get("revision")
        if not isinstance(uid, str) or type(revision) is not int or uid in seen:
            raise AnnotationValidationError("片段选择无效或重复")
        seen.add(uid)
        try:
            row = uow.evidence.get_utterance(uid)
        except KeyError as exc:
            raise AnnotationValidationError("片段已不存在") from exc
        if row.revision != revision or row.status != "active":
            raise UtteranceRevisionConflict("片段已变化，请在审核中核对最新内容")
        if not uow.evidence.annotation_is_current(row, "sound"):
            raise UtteranceRevisionConflict("human fact superseded; refresh before correcting")
        rows.append(row)
    result = []
    for row in rows:
        if (row.evidence.get("sound_kind", "speech") == sound_kind
            and row.evidence.get("annotation_fact_ids", {}).get("sound")
            and "sound" not in row.evidence.get("annotation_review", {}).get("dimensions", {})):
            result.append(utterance_dto(row, speaker_label=uow.evidence.speaker_label(row.speaker_track_id),
                original_speaker_label=uow.evidence.speaker_label(row.original_speaker_track_id)))
        else:
            result.append(apply_utterance_correction(uow, CorrectUtteranceCommand(
                row.utterance_id, row.revision, row.text, actor, sound_kind=sound_kind), now))
    return {"utterances": result}
