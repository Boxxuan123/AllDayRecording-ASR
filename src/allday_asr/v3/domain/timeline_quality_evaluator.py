from __future__ import annotations
from collections import Counter, defaultdict
from typing import Any, Mapping, Sequence
from unicodedata import category, normalize

from .timeline_quality_parser import _require_unique
from .timeline_quality_types import (
    ChunkSeam,
    ReferenceUtterance,
    PredictedUtterance,
    TimelineTruthCompleteness,
    TimelineQualityPolicy,
    TimelineQualityDecision,
    _MatchedUtterance,
)


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
    _require_unique(
        (item.utterance_id for item in references), "reference utterance ids"
    )
    _require_unique(
        (item.utterance_id for item in predictions), "prediction utterance ids"
    )
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
        value for item in matched for value in (item.start_error_ms, item.end_error_ms)
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
        if speaker_mapping.get(item.prediction.speaker_id) != item.reference.speaker_id
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
        (
            "missed_reference_rate_exceeds_limit",
            metrics["missed_reference_rate"],
            policy.max_missed_reference_rate,
        ),
        (
            "extra_prediction_rate_exceeds_limit",
            metrics["extra_prediction_rate"],
            policy.max_extra_prediction_rate,
        ),
        (
            "gross_misalignment_rate_exceeds_limit",
            metrics["gross_misalignment_rate"],
            policy.max_gross_misalignment_rate,
        ),
        (
            "seam_cer_exceeds_limit",
            metrics["asr"]["character_error_rate"],
            policy.max_seam_cer,
        ),
        (
            "speaker_confusion_rate_exceeds_limit",
            metrics["speaker"]["confusion_rate"],
            policy.max_speaker_confusion_rate,
        ),
        (
            "speaker_continuity_error_rate_exceeds_limit",
            metrics["speaker"]["continuity_error_rate"],
            policy.max_speaker_continuity_error_rate,
        ),
        (
            "overlap_error_rate_exceeds_limit",
            overlap["error_rate"],
            policy.max_overlap_error_rate,
        ),
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
        [
            item
            for index, item in enumerate(predictions)
            if index not in used_predictions
        ],
    )


def _speaker_mapping(matches: Sequence[_MatchedUtterance]) -> dict[str, str]:
    weights: dict[str, Counter[str]] = defaultdict(Counter)
    for item in matches:
        weights[item.prediction.speaker_id][item.reference.speaker_id] += (
            item.overlap_ms
        )
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
    errors = sum(
        not bool(value["left"].intersection(value["right"])) for value in eligible
    )
    return len(eligible), errors


def _near_seam(values: Sequence[Any], seam_offset_ms: int, window_ms: int) -> list[Any]:
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
