from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Sequence


class PersonKind(StrEnum):
    SELF = "self"
    KNOWN = "known"
    UNKNOWN = "unknown"


class ClusterStatus(StrEnum):
    ACTIVE = "active"
    MERGED = "merged"
    SPLIT = "split"
    IGNORED = "ignored"


class PrototypeStatus(StrEnum):
    CANDIDATE = "candidate"
    ACCEPTED = "accepted"
    REVOKED = "revoked"


class PersonOperationKind(StrEnum):
    ANALYZE = "analyze"
    LABEL = "label"
    MERGE = "merge"
    SPLIT = "split"
    IGNORE = "ignore"
    UNDO = "undo"


class SpeakerMatchTier(StrEnum):
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    AUTO_MATCHED = "auto_matched"
    SUGGESTED = "suggested"
    NO_KNOWN_MATCH = "no_known_match"


class PersonIdentityMaturity(StrEnum):
    SEED = "seed"
    LEARNING = "learning"
    CALIBRATED = "calibrated"
    SUSPENDED = "suspended"


@dataclass(frozen=True)
class Person:
    person_id: str
    display_name: str
    kind: PersonKind
    user_confirmed: bool
    revision: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.display_name.strip():
            raise ValueError("person display name is required")
        if self.revision < 1:
            raise ValueError("person revision must be positive")
        if self.kind is PersonKind.UNKNOWN and self.user_confirmed:
            raise ValueError("anonymous persons cannot be user-confirmed contacts")


@dataclass(frozen=True)
class SpeakerCluster:
    cluster_id: str
    display_label: str
    status: ClusterStatus
    revision: int
    suggested_person_id: str | None
    suggestion_confidence: float | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.display_label.strip():
            raise ValueError("cluster display label is required")
        if self.revision < 1:
            raise ValueError("cluster revision must be positive")
        if self.suggested_person_id is None and self.suggestion_confidence is not None:
            raise ValueError("cluster confidence requires a suggested person")
        if self.suggestion_confidence is not None and not 0 <= self.suggestion_confidence <= 1:
            raise ValueError("cluster suggestion confidence is invalid")


@dataclass(frozen=True)
class RepresentativeClip:
    media_id: str
    start_ms: int
    end_ms: int
    utterance_id: str | None = None

    def __post_init__(self) -> None:
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("representative clip coordinates are invalid")


@dataclass(frozen=True)
class SpeakerEmbedding:
    speaker_track_id: str
    model: str
    model_version: str
    vector: tuple[float, ...]
    representatives: tuple[RepresentativeClip, ...]
    quality_score: float

    def __post_init__(self) -> None:
        if not self.vector or not all(math.isfinite(value) for value in self.vector):
            raise ValueError("speaker embedding vector is invalid")
        if not 0 <= self.quality_score <= 1:
            raise ValueError("speaker embedding quality is invalid")


@dataclass(frozen=True)
class ClusterMatchPolicy:
    anonymous_threshold: float = 0.82
    person_suggestion_threshold: float = 0.92
    minimum_margin: float = 0.05

    def __post_init__(self) -> None:
        if not 0 <= self.anonymous_threshold <= 1:
            raise ValueError("anonymous match threshold is invalid")
        if not 0 <= self.person_suggestion_threshold <= 1:
            raise ValueError("person suggestion threshold is invalid")
        if not 0 <= self.minimum_margin <= 1:
            raise ValueError("speaker match margin is invalid")


@dataclass(frozen=True)
class PersonIdentityPolicy:
    person_id: str
    revision: int = 1
    maturity_status: PersonIdentityMaturity = PersonIdentityMaturity.SEED
    auto_match_enabled: bool = False
    suggest_threshold: float = 0.82
    auto_accept_threshold: float = 0.92
    minimum_margin: float = 0.05
    minimum_quality: float = 0.50

    def __post_init__(self) -> None:
        if not self.person_id:
            raise ValueError("person identity policy requires person_id")
        if self.revision < 1:
            raise ValueError("person identity policy revision must be positive")
        if not -1 <= self.suggest_threshold <= 1:
            raise ValueError("person suggestion threshold is invalid")
        if not -1 <= self.auto_accept_threshold <= 1:
            raise ValueError("person automatic threshold is invalid")
        if self.auto_accept_threshold < self.suggest_threshold:
            raise ValueError("automatic threshold must not be below suggestion threshold")
        if not 0 <= self.minimum_margin <= 1:
            raise ValueError("person identity margin is invalid")
        if not 0 <= self.minimum_quality <= 1:
            raise ValueError("person identity quality threshold is invalid")
        if self.auto_match_enabled and self.maturity_status is not PersonIdentityMaturity.CALIBRATED:
            raise ValueError("only calibrated people may enable automatic matching")


@dataclass(frozen=True)
class MatchDecision:
    target_id: str | None
    score: float | None
    reason: str


@dataclass(frozen=True)
class LayeredMatchDecision:
    tier: SpeakerMatchTier
    candidate_person_id: str | None
    best_score: float | None
    second_best_score: float | None
    score_margin: float | None
    policy_revision: int | None
    reason: str


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or len(left) != len(right):
        raise ValueError("speaker vectors must have the same non-zero dimension")
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        raise ValueError("speaker vectors cannot have zero norm")
    return max(-1.0, min(1.0, numerator / (left_norm * right_norm)))


def conservative_match(
    vector: Sequence[float],
    candidates: Sequence[tuple[str, Sequence[float]]],
    *,
    threshold: float,
    minimum_margin: float,
) -> MatchDecision:
    """Return no match whenever score or separation is not strong enough."""

    best_by_target: dict[str, float] = {}
    for candidate_id, candidate in candidates:
        score = cosine_similarity(vector, candidate)
        best_by_target[candidate_id] = max(score, best_by_target.get(candidate_id, -1.0))
    scored = sorted(best_by_target.items(), key=lambda item: (-item[1], item[0]))
    if not scored:
        return MatchDecision(None, None, "no_candidates")
    best_id, best_score = scored[0]
    if best_score < threshold:
        return MatchDecision(None, best_score, "below_threshold")
    if len(scored) > 1 and best_score - scored[1][1] < minimum_margin:
        return MatchDecision(None, best_score, "ambiguous")
    return MatchDecision(best_id, best_score, "matched")


def layered_person_match(
    vector: Sequence[float],
    candidates: Sequence[tuple[str, Sequence[float]]],
    policies: dict[str, PersonIdentityPolicy],
    *,
    quality_score: float,
) -> LayeredMatchDecision:
    """Classify a known-person match without treating weak audio as another person."""

    if not 0 <= quality_score <= 1:
        raise ValueError("speaker match quality is invalid")
    best_by_person: dict[str, float] = {}
    for person_id, candidate in candidates:
        score = cosine_similarity(vector, candidate)
        best_by_person[person_id] = max(score, best_by_person.get(person_id, -1.0))
    scored = sorted(best_by_person.items(), key=lambda item: (-item[1], item[0]))
    if not scored:
        return LayeredMatchDecision(
            SpeakerMatchTier.NO_KNOWN_MATCH,
            None,
            None,
            None,
            None,
            None,
            "no_registered_people",
        )
    person_id, best_score = scored[0]
    second_score = scored[1][1] if len(scored) > 1 else None
    margin = best_score - second_score if second_score is not None else None
    policy = policies.get(person_id)
    if policy is None:
        return LayeredMatchDecision(
            SpeakerMatchTier.NO_KNOWN_MATCH,
            person_id,
            best_score,
            second_score,
            margin,
            None,
            "person_policy_missing",
        )
    if quality_score < policy.minimum_quality:
        return LayeredMatchDecision(
            SpeakerMatchTier.INSUFFICIENT_EVIDENCE,
            None,
            None,
            None,
            None,
            None,
            "track_quality_below_threshold",
        )
    separated = margin is None or margin >= policy.minimum_margin
    if (
        best_score >= policy.auto_accept_threshold
        and separated
        and policy.auto_match_enabled
        and policy.maturity_status is PersonIdentityMaturity.CALIBRATED
    ):
        return LayeredMatchDecision(
            SpeakerMatchTier.AUTO_MATCHED,
            person_id,
            best_score,
            second_score,
            margin,
            policy.revision,
            "calibrated_score_above_auto_threshold",
        )
    if best_score >= policy.suggest_threshold:
        reason = (
            "score_above_suggest_threshold"
            if separated
            else "score_above_suggest_threshold_but_ambiguous"
        )
        return LayeredMatchDecision(
            SpeakerMatchTier.SUGGESTED,
            person_id,
            best_score,
            second_score,
            margin,
            policy.revision,
            reason,
        )
    return LayeredMatchDecision(
        SpeakerMatchTier.NO_KNOWN_MATCH,
        person_id,
        best_score,
        second_score,
        margin,
        policy.revision,
        "score_below_suggest_threshold",
    )


__all__ = [
    "ClusterMatchPolicy",
    "ClusterStatus",
    "LayeredMatchDecision",
    "MatchDecision",
    "Person",
    "PersonIdentityMaturity",
    "PersonIdentityPolicy",
    "PersonKind",
    "PersonOperationKind",
    "PrototypeStatus",
    "RepresentativeClip",
    "SpeakerCluster",
    "SpeakerEmbedding",
    "SpeakerMatchTier",
    "conservative_match",
    "cosine_similarity",
    "layered_person_match",
]
