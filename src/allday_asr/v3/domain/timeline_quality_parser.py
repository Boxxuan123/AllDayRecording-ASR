from __future__ import annotations
from typing import Any, Mapping, Sequence

from .timeline_quality_types import (
    TIMELINE_AUDIT_FORMAT,
    ChunkSeam,
    ReferenceUtterance,
    PredictedUtterance,
    TimelineTruthCompleteness,
)


def parse_timeline_audit_document(
    value: Mapping[str, Any],
) -> tuple[
    str,
    TimelineTruthCompleteness,
    tuple[ChunkSeam, ...],
    tuple[ReferenceUtterance, ...],
    tuple[PredictedUtterance, ...],
]:
    required = {
        "format",
        "session_id",
        "truth_completeness",
        "seams",
        "reference_utterances",
        "predicted_utterances",
        "metadata",
    }
    if set(value) != required:
        raise ValueError("timeline audit document fields are invalid")
    if value["format"] != TIMELINE_AUDIT_FORMAT:
        raise ValueError("timeline audit document format is unsupported")
    session_id = value["session_id"]
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("timeline audit session id is required")
    if not isinstance(value["metadata"], Mapping):
        raise ValueError("timeline audit metadata must be an object")
    completeness_value = _object(value["truth_completeness"], "truth completeness")
    if set(completeness_value) != {"alignment", "transcript", "speaker", "overlap"}:
        raise ValueError("timeline truth completeness fields are invalid")
    completeness = TimelineTruthCompleteness(
        alignment=_string(completeness_value["alignment"], "alignment completeness"),
        transcript=_string(completeness_value["transcript"], "transcript completeness"),
        speaker=_string(completeness_value["speaker"], "speaker completeness"),
        overlap=_string(completeness_value["overlap"], "overlap completeness"),
    )
    seams = tuple(_parse_seam(item) for item in _list(value["seams"], "seams"))
    references = tuple(
        _parse_reference(item)
        for item in _list(value["reference_utterances"], "reference utterances")
    )
    predictions = tuple(
        _parse_prediction(item)
        for item in _list(value["predicted_utterances"], "predicted utterances")
    )
    _require_unique((item.seam_id for item in seams), "timeline seam ids")
    _require_unique(
        (item.utterance_id for item in references), "reference utterance ids"
    )
    _require_unique(
        (item.utterance_id for item in predictions), "prediction utterance ids"
    )
    return session_id, completeness, seams, references, predictions


def _parse_seam(value: Any) -> ChunkSeam:
    item = _object(value, "seam")
    if set(item) != {
        "seam_id",
        "offset_ms",
        "left_chunk_id",
        "right_chunk_id",
        "reviewed",
    }:
        raise ValueError("timeline seam fields are invalid")
    if not isinstance(item["reviewed"], bool):
        raise ValueError("timeline seam reviewed must be boolean")
    return ChunkSeam(
        seam_id=_string(item["seam_id"], "seam id"),
        offset_ms=_integer(item["offset_ms"], "seam offset"),
        left_chunk_id=_string(item["left_chunk_id"], "left chunk id"),
        right_chunk_id=_string(item["right_chunk_id"], "right chunk id"),
        reviewed=item["reviewed"],
    )


def _parse_reference(value: Any) -> ReferenceUtterance:
    item = _object(value, "reference utterance")
    if set(item) != {
        "utterance_id",
        "start_ms",
        "end_ms",
        "text",
        "speaker_id",
        "has_overlap",
    }:
        raise ValueError("reference utterance fields are invalid")
    if not isinstance(item["has_overlap"], bool):
        raise ValueError("reference overlap marker must be boolean")
    return ReferenceUtterance(
        utterance_id=_string(item["utterance_id"], "reference utterance id"),
        start_ms=_integer(item["start_ms"], "reference start"),
        end_ms=_integer(item["end_ms"], "reference end"),
        text=_string(item["text"], "reference text"),
        speaker_id=_string(item["speaker_id"], "reference speaker"),
        has_overlap=item["has_overlap"],
    )


def _parse_prediction(value: Any) -> PredictedUtterance:
    item = _object(value, "predicted utterance")
    if set(item) != {
        "utterance_id",
        "start_ms",
        "end_ms",
        "text",
        "speaker_id",
        "source_chunk_ids",
    }:
        raise ValueError("predicted utterance fields are invalid")
    source_chunks = tuple(
        _string(value, "prediction source chunk")
        for value in _list(item["source_chunk_ids"], "prediction source chunks")
    )
    return PredictedUtterance(
        utterance_id=_string(item["utterance_id"], "predicted utterance id"),
        start_ms=_integer(item["start_ms"], "prediction start"),
        end_ms=_integer(item["end_ms"], "prediction end"),
        text=_string(item["text"], "prediction text"),
        speaker_id=_string(item["speaker_id"], "prediction speaker"),
        source_chunk_ids=source_chunks,
    )


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    return value


def _integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    return value


def _require_unique(values: Sequence[str] | Any, label: str) -> None:
    materialized = list(values)
    if len(set(materialized)) != len(materialized):
        raise ValueError(f"{label} must be unique")
