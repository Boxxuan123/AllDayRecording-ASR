from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence
from unicodedata import category, normalize


TIMELINE_AUDIT_FORMAT = "AllDayRecording V3.1 timeline seam audit v1"
TIMELINE_POLICY_VERSION = "v3.1-c.1"


@dataclass(frozen=True)
class ChunkSeam:
    seam_id: str
    offset_ms: int
    left_chunk_id: str
    right_chunk_id: str
    reviewed: bool

    def __post_init__(self) -> None:
        if not self.seam_id.strip():
            raise ValueError("timeline seam id is required")
        if self.offset_ms < 1:
            raise ValueError("timeline seam offset must be positive")
        if not self.left_chunk_id.strip() or not self.right_chunk_id.strip():
            raise ValueError("timeline seam chunk ids are required")
        if self.left_chunk_id == self.right_chunk_id:
            raise ValueError("timeline seam must join two different chunks")


@dataclass(frozen=True)
class ReferenceUtterance:
    utterance_id: str
    start_ms: int
    end_ms: int
    text: str
    speaker_id: str
    has_overlap: bool

    def __post_init__(self) -> None:
        _validate_utterance_span(
            self.utterance_id, self.start_ms, self.end_ms, self.text, self.speaker_id
        )
        if not self.text.strip():
            raise ValueError("reference utterance text is required")


@dataclass(frozen=True)
class PredictedUtterance:
    utterance_id: str
    start_ms: int
    end_ms: int
    text: str
    speaker_id: str
    source_chunk_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_utterance_span(
            self.utterance_id, self.start_ms, self.end_ms, self.text, self.speaker_id
        )
        if not self.source_chunk_ids or any(
            not value.strip() for value in self.source_chunk_ids
        ):
            raise ValueError("prediction source chunk ids are required")
        if len(set(self.source_chunk_ids)) != len(self.source_chunk_ids):
            raise ValueError("prediction source chunk ids must be unique")


@dataclass(frozen=True)
class TimelineTruthCompleteness:
    alignment: str
    transcript: str
    speaker: str
    overlap: str

    def blockers(self) -> list[str]:
        return [
            f"{name}_truth_not_exhaustive"
            for name, value in asdict(self).items()
            if value != "exhaustive"
        ]


@dataclass(frozen=True)
class TimelineQualityPolicy:
    policy_version: str = TIMELINE_POLICY_VERSION
    seam_window_ms: int = 5_000
    min_reviewed_seams: int = 10
    min_reference_utterances: int = 30
    min_overlap_utterances: int = 5
    gross_boundary_error_ms: int = 1_500
    gross_utterance_cer: float = 0.50
    max_missed_reference_rate: float = 0.05
    max_extra_prediction_rate: float = 0.05
    max_gross_misalignment_rate: float = 0.05
    max_p95_boundary_error_ms: int = 750
    max_seam_cer: float = 0.20
    max_speaker_confusion_rate: float = 0.05
    max_speaker_continuity_error_rate: float = 0.05
    max_overlap_error_rate: float = 0.20

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise ValueError("timeline quality policy version is required")
        if min(
            self.seam_window_ms,
            self.min_reviewed_seams,
            self.min_reference_utterances,
            self.min_overlap_utterances,
            self.gross_boundary_error_ms,
            self.max_p95_boundary_error_ms,
        ) < 1:
            raise ValueError("timeline quality policy counts must be positive")
        for value in (
            self.gross_utterance_cer,
            self.max_missed_reference_rate,
            self.max_extra_prediction_rate,
            self.max_gross_misalignment_rate,
            self.max_seam_cer,
            self.max_speaker_confusion_rate,
            self.max_speaker_continuity_error_rate,
            self.max_overlap_error_rate,
        ):
            if not 0 <= value <= 1:
                raise ValueError("timeline quality policy rates must be between 0 and 1")


@dataclass(frozen=True)
class TimelineQualityDecision:
    accepted: bool
    blockers: tuple[str, ...]
    metrics: dict[str, Any]
    policy: TimelineQualityPolicy


@dataclass(frozen=True)
class _MatchedUtterance:
    seam_id: str
    seam_offset_ms: int
    reference: ReferenceUtterance
    prediction: PredictedUtterance
    overlap_ms: int
    start_error_ms: int
    end_error_ms: int
    edit_distance: int
    reference_characters: int

    @property
    def utterance_cer(self) -> float:
        return self.edit_distance / self.reference_characters


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
    _require_unique((item.utterance_id for item in references), "reference utterance ids")
    _require_unique((item.utterance_id for item in predictions), "prediction utterance ids")
    return session_id, completeness, seams, references, predictions


def evaluate_timeline_quality(
    seams: Sequence[ChunkSeam],
    references: Sequence[ReferenceUtterance],
    predictions: Sequence[PredictedUtterance],
    completeness: TimelineTruthCompleteness,
    *,
    policy: TimelineQualityPolicy | None = None,
) -> TimelineQualityDecision:
    selected_policy = policy or TimelineQualityPolicy()
    blockers = completeness.blockers()
    _require_unique((item.seam_id for item in seams), "timeline seam ids")
    _require_unique((item.utterance_id for item in references), "reference utterance ids")
    _require_unique((item.utterance_id for item in predictions), "prediction utterance ids")
    ordered_seams = sorted(seams, key=lambda item: (item.offset_ms, item.seam_id))
    if any(
        right.offset_ms - left.offset_ms < selected_policy.seam_window_ms * 2
        for left, right in zip(ordered_seams, ordered_seams[1:], strict=False)
    ):
        blockers.append("seam_windows_overlap")
    unreviewed = [seam.seam_id for seam in seams if not seam.reviewed]
    if unreviewed:
        blockers.append("seams_not_fully_reviewed")

    matched: list[_MatchedUtterance] = []
    missed: list[tuple[str, ReferenceUtterance]] = []
    extra: list[tuple[str, PredictedUtterance]] = []
    seam_reference_count = 0
    seam_prediction_count = 0
    for seam in seams:
        scoped_references = _near_seam(
            references, seam.offset_ms, selected_policy.seam_window_ms
        )
        scoped_predictions = _near_seam(
            predictions, seam.offset_ms, selected_policy.seam_window_ms
        )
        seam_chunks = {seam.left_chunk_id, seam.right_chunk_id}
        if any(
            not seam_chunks.intersection(value.source_chunk_ids)
            for value in scoped_predictions
        ):
            blockers.append("prediction_chunk_provenance_mismatch")
        seam_reference_count += len(scoped_references)
        seam_prediction_count += len(scoped_predictions)
        seam_matches, seam_missed, seam_extra = _match_utterances(
            seam, scoped_references, scoped_predictions
        )
        matched.extend(seam_matches)
        missed.extend((seam.seam_id, item) for item in seam_missed)
        extra.extend((seam.seam_id, item) for item in seam_extra)

    boundary_errors = [
        value
        for item in matched
        for value in (item.start_error_ms, item.end_error_ms)
    ]
    total_edits = sum(item.edit_distance for item in matched) + sum(
        len(_normalized_text(item.text)) for _, item in missed
    )
    total_reference_characters = sum(
        len(_normalized_text(item.text))
        for seam in seams
        for item in _near_seam(
            references, seam.offset_ms, selected_policy.seam_window_ms
        )
    )
    gross_keys = {
        (item.seam_id, item.reference.utterance_id)
        for item in matched
        if max(item.start_error_ms, item.end_error_ms)
        > selected_policy.gross_boundary_error_ms
        or item.utterance_cer > selected_policy.gross_utterance_cer
    }
    gross_keys.update((seam_id, item.utterance_id) for seam_id, item in missed)

    speaker_mapping = _speaker_mapping(matched)
    speaker_mismatch_keys = {
        (item.seam_id, item.reference.utterance_id)
        for item in matched
        if speaker_mapping.get(item.prediction.speaker_id)
        != item.reference.speaker_id
    }
    continuity_checks, continuity_errors = _speaker_continuity(matched)
    overlap_reference_keys = {
        (seam.seam_id, item.utterance_id)
        for seam in seams
        for item in _near_seam(
            references, seam.offset_ms, selected_policy.seam_window_ms
        )
        if item.has_overlap
    }
    overlap_error_keys = overlap_reference_keys.intersection(
        gross_keys | speaker_mismatch_keys
    )

    metrics: dict[str, Any] = {
        "reviewed_seams": sum(seam.reviewed for seam in seams),
        "seam_count": len(seams),
        "reference_utterances": seam_reference_count,
        "predicted_utterances": seam_prediction_count,
        "matched_utterances": len(matched),
        "missed_reference_rate": _ratio(len(missed), seam_reference_count),
        "extra_prediction_rate": _ratio(len(extra), seam_reference_count),
        "gross_misalignment_rate": _ratio(len(gross_keys), seam_reference_count),
        "boundary": {
            "mean_absolute_error_ms": _mean(boundary_errors),
            "p95_absolute_error_ms": _percentile(boundary_errors, 0.95),
            "max_absolute_error_ms": max(boundary_errors) if boundary_errors else None,
        },
        "asr": {
            "character_error_rate": _ratio(total_edits, total_reference_characters),
            "edit_distance": total_edits,
            "reference_characters": total_reference_characters,
        },
        "speaker": {
            "confusion_rate": _ratio(len(speaker_mismatch_keys), len(matched)),
            "continuity_checks": continuity_checks,
            "continuity_errors": continuity_errors,
            "continuity_error_rate": _ratio(continuity_errors, continuity_checks),
        },
        "overlap": {
            "reference_utterances": len(overlap_reference_keys),
            "error_utterances": len(overlap_error_keys),
            "error_rate": _ratio(len(overlap_error_keys), len(overlap_reference_keys)),
        },
    }
    blockers.extend(_metric_blockers(metrics, selected_policy))
    return TimelineQualityDecision(
        accepted=not blockers,
        blockers=tuple(dict.fromkeys(blockers)),
        metrics=metrics,
        policy=selected_policy,
    )


def _metric_blockers(
    metrics: Mapping[str, Any], policy: TimelineQualityPolicy
) -> list[str]:
    blockers: list[str] = []
    if metrics["reviewed_seams"] < policy.min_reviewed_seams:
        blockers.append("insufficient_reviewed_seams")
    if metrics["reference_utterances"] < policy.min_reference_utterances:
        blockers.append("insufficient_reference_utterances")
    overlap = metrics["overlap"]
    if overlap["reference_utterances"] < policy.min_overlap_utterances:
        blockers.append("insufficient_overlap_utterances")
    checks = (
        ("missed_reference_rate_exceeds_limit", metrics["missed_reference_rate"], policy.max_missed_reference_rate),
        ("extra_prediction_rate_exceeds_limit", metrics["extra_prediction_rate"], policy.max_extra_prediction_rate),
        ("gross_misalignment_rate_exceeds_limit", metrics["gross_misalignment_rate"], policy.max_gross_misalignment_rate),
        ("seam_cer_exceeds_limit", metrics["asr"]["character_error_rate"], policy.max_seam_cer),
        ("speaker_confusion_rate_exceeds_limit", metrics["speaker"]["confusion_rate"], policy.max_speaker_confusion_rate),
        ("speaker_continuity_error_rate_exceeds_limit", metrics["speaker"]["continuity_error_rate"], policy.max_speaker_continuity_error_rate),
        ("overlap_error_rate_exceeds_limit", overlap["error_rate"], policy.max_overlap_error_rate),
    )
    for reason, value, maximum in checks:
        if value is None:
            blockers.append(reason.replace("exceeds_limit", "unavailable"))
        elif value > maximum:
            blockers.append(reason)
    p95 = metrics["boundary"]["p95_absolute_error_ms"]
    if p95 is None:
        blockers.append("boundary_p95_unavailable")
    elif p95 > policy.max_p95_boundary_error_ms:
        blockers.append("boundary_p95_exceeds_limit")
    return blockers


def _match_utterances(
    seam: ChunkSeam,
    references: Sequence[ReferenceUtterance],
    predictions: Sequence[PredictedUtterance],
) -> tuple[list[_MatchedUtterance], list[ReferenceUtterance], list[PredictedUtterance]]:
    candidates: list[tuple[int, int, str, str, int, int]] = []
    for reference_index, reference in enumerate(references):
        for prediction_index, prediction in enumerate(predictions):
            overlap_ms = max(
                0,
                min(reference.end_ms, prediction.end_ms)
                - max(reference.start_ms, prediction.start_ms),
            )
            if overlap_ms:
                boundary_distance = abs(reference.start_ms - prediction.start_ms) + abs(
                    reference.end_ms - prediction.end_ms
                )
                candidates.append(
                    (
                        -overlap_ms,
                        boundary_distance,
                        reference.utterance_id,
                        prediction.utterance_id,
                        reference_index,
                        prediction_index,
                    )
                )
    used_references: set[int] = set()
    used_predictions: set[int] = set()
    matches: list[_MatchedUtterance] = []
    for negative_overlap, _, _, _, reference_index, prediction_index in sorted(
        candidates
    ):
        if reference_index in used_references or prediction_index in used_predictions:
            continue
        used_references.add(reference_index)
        used_predictions.add(prediction_index)
        reference = references[reference_index]
        prediction = predictions[prediction_index]
        reference_text = _normalized_text(reference.text)
        matches.append(
            _MatchedUtterance(
                seam_id=seam.seam_id,
                seam_offset_ms=seam.offset_ms,
                reference=reference,
                prediction=prediction,
                overlap_ms=-negative_overlap,
                start_error_ms=abs(reference.start_ms - prediction.start_ms),
                end_error_ms=abs(reference.end_ms - prediction.end_ms),
                edit_distance=_edit_distance(
                    reference_text, _normalized_text(prediction.text)
                ),
                reference_characters=len(reference_text),
            )
        )
    return (
        matches,
        [item for index, item in enumerate(references) if index not in used_references],
        [item for index, item in enumerate(predictions) if index not in used_predictions],
    )


def _speaker_mapping(matches: Sequence[_MatchedUtterance]) -> dict[str, str]:
    weights: dict[str, Counter[str]] = defaultdict(Counter)
    for item in matches:
        weights[item.prediction.speaker_id][item.reference.speaker_id] += item.overlap_ms
    return {
        prediction: sorted(values.items(), key=lambda item: (-item[1], item[0]))[0][0]
        for prediction, values in weights.items()
    }


def _speaker_continuity(matches: Sequence[_MatchedUtterance]) -> tuple[int, int]:
    grouped: dict[tuple[str, int, str], dict[str, set[str]]] = defaultdict(
        lambda: {"left": set(), "right": set()}
    )
    for item in matches:
        midpoint = (item.reference.start_ms + item.reference.end_ms) / 2
        side = "left" if midpoint < item.seam_offset_ms else "right"
        grouped[(item.seam_id, item.seam_offset_ms, item.reference.speaker_id)][
            side
        ].add(item.prediction.speaker_id)
    eligible = [value for value in grouped.values() if value["left"] and value["right"]]
    errors = sum(not bool(value["left"].intersection(value["right"])) for value in eligible)
    return len(eligible), errors


def _near_seam(
    values: Sequence[Any], seam_offset_ms: int, window_ms: int
) -> list[Any]:
    start = seam_offset_ms - window_ms
    end = seam_offset_ms + window_ms
    return [value for value in values if value.start_ms < end and value.end_ms > start]


def _normalized_text(value: str) -> str:
    return "".join(
        character
        for character in normalize("NFKC", value).casefold()
        if not character.isspace() and not category(character).startswith("P")
    )


def _edit_distance(reference: str, hypothesis: str) -> int:
    previous = list(range(len(hypothesis) + 1))
    for reference_index, reference_character in enumerate(reference, start=1):
        current = [reference_index]
        for hypothesis_index, hypothesis_character in enumerate(hypothesis, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[hypothesis_index] + 1,
                    previous[hypothesis_index - 1]
                    + (reference_character != hypothesis_character),
                )
            )
        previous = current
    return previous[-1]


def _mean(values: Sequence[int]) -> float | None:
    return sum(values) / len(values) if values else None


def _percentile(values: Sequence[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * percentile + 0.999999) - 1))
    return ordered[index]


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _validate_utterance_span(
    utterance_id: str, start_ms: int, end_ms: int, text: str, speaker_id: str
) -> None:
    if not utterance_id.strip() or not speaker_id.strip():
        raise ValueError("timeline utterance id and speaker are required")
    if start_ms < 0 or end_ms <= start_ms:
        raise ValueError("timeline utterance coordinates are invalid")
    if not isinstance(text, str):
        raise ValueError("timeline utterance text must be a string")


def _parse_seam(value: Any) -> ChunkSeam:
    item = _object(value, "seam")
    if set(item) != {"seam_id", "offset_ms", "left_chunk_id", "right_chunk_id", "reviewed"}:
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
    if set(item) != {"utterance_id", "start_ms", "end_ms", "text", "speaker_id", "has_overlap"}:
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
    if set(item) != {"utterance_id", "start_ms", "end_ms", "text", "speaker_id", "source_chunk_ids"}:
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


__all__ = [
    "TIMELINE_AUDIT_FORMAT",
    "TIMELINE_POLICY_VERSION",
    "ChunkSeam",
    "PredictedUtterance",
    "ReferenceUtterance",
    "TimelineQualityDecision",
    "TimelineQualityPolicy",
    "TimelineTruthCompleteness",
    "evaluate_timeline_quality",
    "parse_timeline_audit_document",
]
