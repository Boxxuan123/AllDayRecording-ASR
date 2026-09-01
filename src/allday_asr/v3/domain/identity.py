from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from statistics import median
from typing import Any, Sequence


class SelfIdentity(StrEnum):
    SELF = "self"
    NOT_SELF = "not_self"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class VoiceprintObservation:
    score: float
    duration_ms: int
    has_overlap: bool = False
    media_playback: bool = False

    def __post_init__(self) -> None:
        if not -1.0 <= self.score <= 1.0:
            raise ValueError("voiceprint score must be between -1 and 1")
        if self.duration_ms <= 0:
            raise ValueError("voiceprint observation duration must be positive")


@dataclass(frozen=True)
class VoiceprintCalibration:
    policy_version: str
    self_threshold: float
    not_self_threshold: float
    positive_holdout: int
    negative_holdout: int
    false_accept_rate: float
    false_reject_rate: float
    disjoint_holdout: bool

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise ValueError("identity policy version is required")
        if not -1 <= self.not_self_threshold < self.self_threshold <= 1:
            raise ValueError("identity thresholds must have a non-empty unknown band")
        if self.positive_holdout < 0 or self.negative_holdout < 0:
            raise ValueError("identity holdout counts cannot be negative")
        if not 0 <= self.false_accept_rate <= 1:
            raise ValueError("false accept rate must be between 0 and 1")
        if not 0 <= self.false_reject_rate <= 1:
            raise ValueError("false reject rate must be between 0 and 1")


@dataclass(frozen=True)
class IdentityAcceptancePolicy:
    min_positive_holdout: int = 20
    min_negative_holdout: int = 50
    max_false_accept_rate: float = 0.01
    max_false_reject_rate: float = 0.20
    min_window_ms: int = 2_000
    min_eligible_windows: int = 2

    def __post_init__(self) -> None:
        if self.min_positive_holdout < 1 or self.min_negative_holdout < 1:
            raise ValueError("identity acceptance requires holdout samples")
        if not 0 <= self.max_false_accept_rate <= 1:
            raise ValueError("maximum false accept rate is invalid")
        if not 0 <= self.max_false_reject_rate <= 1:
            raise ValueError("maximum false reject rate is invalid")
        if self.min_window_ms < 1 or self.min_eligible_windows < 1:
            raise ValueError("identity window requirements must be positive")


@dataclass(frozen=True)
class IdentityDecision:
    identity: SelfIdentity
    evidence: dict[str, Any]


@dataclass(frozen=True)
class IdentityHoldoutSample:
    score: float
    identity: SelfIdentity
    session_id: str

    def __post_init__(self) -> None:
        if not -1 <= self.score <= 1:
            raise ValueError("identity holdout score must be between -1 and 1")
        if self.identity is SelfIdentity.UNKNOWN:
            raise ValueError("identity holdout sample must have binary truth")
        if not self.session_id.strip():
            raise ValueError("identity holdout sample requires a session")


def evaluate_voiceprint_calibration(
    samples: Sequence[IdentityHoldoutSample],
    *,
    policy_version: str,
    self_threshold: float,
    not_self_threshold: float,
    enrollment_session_ids: frozenset[str] = frozenset(),
) -> VoiceprintCalibration:
    positives = [sample for sample in samples if sample.identity is SelfIdentity.SELF]
    negatives = [sample for sample in samples if sample.identity is SelfIdentity.NOT_SELF]
    false_accepts = sum(sample.score >= self_threshold for sample in negatives)
    false_rejects = sum(sample.score < self_threshold for sample in positives)
    return VoiceprintCalibration(
        policy_version=policy_version,
        self_threshold=self_threshold,
        not_self_threshold=not_self_threshold,
        positive_holdout=len(positives),
        negative_holdout=len(negatives),
        false_accept_rate=(false_accepts / len(negatives) if negatives else 1.0),
        false_reject_rate=(false_rejects / len(positives) if positives else 1.0),
        disjoint_holdout=not bool(
            enrollment_session_ids.intersection(
                sample.session_id for sample in samples
            )
        ),
    )


def classify_voiceprint_identity(
    observations: Sequence[VoiceprintObservation],
    calibration: VoiceprintCalibration,
    *,
    acceptance: IdentityAcceptancePolicy | None = None,
) -> IdentityDecision:
    """Conservatively project calibrated voiceprint scores to a tri-state identity."""

    acceptance = acceptance or IdentityAcceptancePolicy()
    blocked_reasons = _calibration_blockers(calibration, acceptance)
    eligible = [
        value
        for value in observations
        if value.duration_ms >= acceptance.min_window_ms
        and not value.has_overlap
        and not value.media_playback
    ]
    if len(eligible) < acceptance.min_eligible_windows:
        blocked_reasons.append("insufficient_clean_windows")
    scores = [value.score for value in eligible]
    identity = SelfIdentity.UNKNOWN
    reason = blocked_reasons[0] if blocked_reasons else "score_in_unknown_band"
    if not blocked_reasons:
        if min(scores) >= calibration.self_threshold:
            identity = SelfIdentity.SELF
            reason = "all_clean_windows_above_self_threshold"
        elif max(scores) <= calibration.not_self_threshold:
            identity = SelfIdentity.NOT_SELF
            reason = "all_clean_windows_below_not_self_threshold"
    evidence: dict[str, Any] = {
        "source": "voiceprint",
        "decision": identity.value,
        "reason": reason,
        "policy_version": calibration.policy_version,
        "calibration_accepted": not blocked_reasons,
        "calibration": {
            "self_threshold": calibration.self_threshold,
            "not_self_threshold": calibration.not_self_threshold,
            "positive_holdout": calibration.positive_holdout,
            "negative_holdout": calibration.negative_holdout,
            "false_accept_rate": calibration.false_accept_rate,
            "false_reject_rate": calibration.false_reject_rate,
            "disjoint_holdout": calibration.disjoint_holdout,
        },
        "observation_count": len(observations),
        "eligible_window_count": len(eligible),
        "excluded_window_count": len(observations) - len(eligible),
    }
    if scores:
        evidence["score_summary"] = {
            "min": min(scores),
            "median": median(scores),
            "max": max(scores),
        }
    if blocked_reasons:
        evidence["blocked_reasons"] = blocked_reasons
    return IdentityDecision(identity, evidence)


def unknown_identity_evidence(reason: str = "no_identity_evidence") -> dict[str, Any]:
    return {
        "source": "none",
        "decision": SelfIdentity.UNKNOWN.value,
        "reason": reason,
    }


def _calibration_blockers(
    calibration: VoiceprintCalibration,
    acceptance: IdentityAcceptancePolicy,
) -> list[str]:
    reasons: list[str] = []
    if not calibration.disjoint_holdout:
        reasons.append("holdout_not_disjoint")
    if calibration.positive_holdout < acceptance.min_positive_holdout:
        reasons.append("insufficient_positive_holdout")
    if calibration.negative_holdout < acceptance.min_negative_holdout:
        reasons.append("insufficient_negative_holdout")
    if calibration.false_accept_rate > acceptance.max_false_accept_rate:
        reasons.append("false_accept_rate_exceeds_limit")
    if calibration.false_reject_rate > acceptance.max_false_reject_rate:
        reasons.append("false_reject_rate_exceeds_limit")
    return reasons


__all__ = [
    "IdentityAcceptancePolicy",
    "IdentityDecision",
    "IdentityHoldoutSample",
    "SelfIdentity",
    "VoiceprintCalibration",
    "VoiceprintObservation",
    "classify_voiceprint_identity",
    "evaluate_voiceprint_calibration",
    "unknown_identity_evidence",
]
