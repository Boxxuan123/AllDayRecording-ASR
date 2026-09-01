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
class MatchDecision:
    target_id: str | None
    score: float | None
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


__all__ = [
    "ClusterMatchPolicy",
    "ClusterStatus",
    "MatchDecision",
    "Person",
    "PersonKind",
    "PersonOperationKind",
    "PrototypeStatus",
    "RepresentativeClip",
    "SpeakerCluster",
    "SpeakerEmbedding",
    "conservative_match",
    "cosine_similarity",
]
