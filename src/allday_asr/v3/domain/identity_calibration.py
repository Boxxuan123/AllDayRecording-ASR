from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from allday_asr.v3.domain.identity import (
    IdentityAcceptancePolicy,
    SelfIdentity,
    VoiceprintCalibration,
)


_CLEAN_LABELS = {
    "self": SelfIdentity.SELF,
    "mother": SelfIdentity.NOT_SELF,
    "father": SelfIdentity.NOT_SELF,
    "other_live": SelfIdentity.NOT_SELF,
}


@dataclass(frozen=True)
class CalibrationWindow:
    sample_id: str
    session_id: int
    session_key: str
    start_ms: int
    end_ms: int
    identity: SelfIdentity
    origin: str
    provenance: tuple[str, ...]

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass(frozen=True)
class ThresholdMetrics:
    positives: int
    negatives: int
    true_accepts: int
    false_rejects: int
    false_accepts: int
    true_rejects: int
    false_accept_rate: float
    false_reject_rate: float
    true_accept_rate: float
    true_reject_rate: float
    auc: float
    not_self_coverage: float
    self_misreject_rate: float


def merge_calibration_windows(
    bundle: Mapping[str, Any],
    progress: Mapping[str, Any],
) -> list[CalibrationWindow]:
    sessions = {
        int(value["session_id"]): str(value["session_key"])
        for value in bundle.get("sessions", [])
        if isinstance(value, dict)
        and value.get("session_id") is not None
        and value.get("session_key")
    }
    selected: list[CalibrationWindow] = []
    for raw in bundle.get("effective_identity_windows", []):
        if not isinstance(raw, dict) or str(raw.get("identity")) not in {
            "self",
            "not_self",
        }:
            continue
        session_id = int(raw["session_id"])
        selected.append(
            CalibrationWindow(
                sample_id=str(raw["window_id"]),
                session_id=session_id,
                session_key=sessions.get(session_id, f"session:{session_id}"),
                start_ms=int(raw["start_ms"]),
                end_ms=int(raw["end_ms"]),
                identity=SelfIdentity(str(raw["identity"])),
                origin="legacy_migration",
                provenance=tuple(
                    str(value) for value in raw.get("provenance_record_ids", [])
                ),
            )
        )
    candidates = {
        str(value["candidate_id"]): value
        for value in progress.get("identity_candidates", [])
        if isinstance(value, dict) and value.get("candidate_id")
    }
    temporary: list[CalibrationWindow] = []
    annotations = progress.get("identity_annotations", {})
    if not isinstance(annotations, dict):
        annotations = {}
    for candidate_id, annotation in annotations.items():
        if not isinstance(annotation, dict):
            continue
        identity = _CLEAN_LABELS.get(str(annotation.get("label") or ""))
        candidate = candidates.get(str(candidate_id))
        if identity is None or candidate is None:
            continue
        session_id = int(candidate["session_id"])
        session_key = str(
            candidate.get("session_key")
            or sessions.get(session_id)
            or f"session:{session_id}"
        )
        temporary.append(
            CalibrationWindow(
                sample_id=f"temporary:{candidate_id}",
                session_id=session_id,
                session_key=session_key,
                start_ms=int(annotation["start_ms"]),
                end_ms=int(annotation["end_ms"]),
                identity=identity,
                origin="v31_temporary_human",
                provenance=(str(candidate_id),),
            )
        )
    temporary.sort(
        key=lambda value: (
            value.identity is not SelfIdentity.SELF,
            value.session_id,
            value.start_ms,
        )
    )
    for value in temporary:
        if value.duration_ms < 2_000 or value.duration_ms > 5_000:
            continue
        if any(
            existing.session_id == value.session_id
            and value.start_ms < existing.end_ms + 500
            and value.end_ms > existing.start_ms - 500
            for existing in selected
        ):
            continue
        selected.append(value)
    selected.sort(key=lambda value: (value.session_id, value.start_ms, value.sample_id))
    return selected


def split_adaptation_windows(
    windows: Sequence[CalibrationWindow],
    adaptation_session_ids: frozenset[int],
) -> tuple[list[CalibrationWindow], list[CalibrationWindow]]:
    adaptation = [
        value
        for value in windows
        if value.session_id in adaptation_session_ids
        and value.identity is SelfIdentity.SELF
    ]
    holdout = [
        value for value in windows if value.session_id not in adaptation_session_ids
    ]
    return adaptation, holdout


def select_self_threshold(
    positive_scores: Sequence[float],
    negative_scores: Sequence[float],
    *,
    max_false_accept_rate: float,
) -> float:
    if not positive_scores or not negative_scores:
        raise ValueError("threshold selection requires positive and negative scores")
    candidates = sorted(
        {
            -1.0,
            1.0,
            *(float(value) for value in positive_scores),
            *(float(value) for value in negative_scores),
            *(
                math.nextafter(float(value), 1.0)
                for value in negative_scores
            ),
        }
    )
    for threshold in candidates:
        false_accept_rate = sum(
            value >= threshold for value in negative_scores
        ) / len(negative_scores)
        if false_accept_rate <= max_false_accept_rate:
            return threshold
    return 1.0


def select_not_self_threshold(
    positive_scores: Sequence[float],
    *,
    self_threshold: float,
) -> float:
    if not positive_scores:
        raise ValueError("not-self threshold selection requires positive scores")
    threshold = math.nextafter(min(positive_scores), -1.0)
    if threshold >= self_threshold:
        threshold = math.nextafter(self_threshold, -1.0)
    return max(-1.0, threshold)


def calculate_threshold_metrics(
    positive_scores: Sequence[float],
    negative_scores: Sequence[float],
    *,
    self_threshold: float,
    not_self_threshold: float,
) -> ThresholdMetrics:
    if not positive_scores or not negative_scores:
        raise ValueError("threshold metrics require positive and negative scores")
    positives = len(positive_scores)
    negatives = len(negative_scores)
    true_accepts = sum(value >= self_threshold for value in positive_scores)
    false_rejects = positives - true_accepts
    false_accepts = sum(value >= self_threshold for value in negative_scores)
    true_rejects = negatives - false_accepts
    not_self_accepted = sum(value <= not_self_threshold for value in negative_scores)
    self_misrejects = sum(value <= not_self_threshold for value in positive_scores)
    return ThresholdMetrics(
        positives=positives,
        negatives=negatives,
        true_accepts=true_accepts,
        false_rejects=false_rejects,
        false_accepts=false_accepts,
        true_rejects=true_rejects,
        false_accept_rate=false_accepts / negatives,
        false_reject_rate=false_rejects / positives,
        true_accept_rate=true_accepts / positives,
        true_reject_rate=true_rejects / negatives,
        auc=_auc(positive_scores, negative_scores),
        not_self_coverage=not_self_accepted / negatives,
        self_misreject_rate=self_misrejects / positives,
    )


def calibration_blockers(
    calibration: VoiceprintCalibration,
    acceptance: IdentityAcceptancePolicy,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if not calibration.disjoint_holdout:
        blockers.append("holdout_not_disjoint")
    if calibration.positive_holdout < acceptance.min_positive_holdout:
        blockers.append("insufficient_positive_holdout")
    if calibration.negative_holdout < acceptance.min_negative_holdout:
        blockers.append("insufficient_negative_holdout")
    if calibration.false_accept_rate > acceptance.max_false_accept_rate:
        blockers.append("false_accept_rate_exceeds_limit")
    if calibration.false_reject_rate > acceptance.max_false_reject_rate:
        blockers.append("false_reject_rate_exceeds_limit")
    return tuple(blockers)


def _auc(positive_scores: Sequence[float], negative_scores: Sequence[float]) -> float:
    wins = 0.0
    for positive in positive_scores:
        for negative in negative_scores:
            if positive > negative:
                wins += 1.0
            elif positive == negative:
                wins += 0.5
    return wins / (len(positive_scores) * len(negative_scores))


__all__ = [
    "CalibrationWindow",
    "ThresholdMetrics",
    "calculate_threshold_metrics",
    "calibration_blockers",
    "merge_calibration_windows",
    "select_not_self_threshold",
    "select_self_threshold",
    "split_adaptation_windows",
]
