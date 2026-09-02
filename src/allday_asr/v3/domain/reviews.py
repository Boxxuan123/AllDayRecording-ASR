from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal


VoiceReviewLane = Literal["primary", "training"]


class ReviewDisposition(StrEnum):
    """Route generated candidates before they reach the human inbox."""

    REVIEW_REQUIRED = "review_required"
    OPTIONAL_LEARNING = "optional_learning"
    SUPPRESS = "suppress"


MEANINGFUL_MEMORY_KINDS = frozenset({"stable_fact", "preference", "commitment"})
MEANINGFUL_EVENT_KINDS = frozenset({"decision", "person_fact"})
MIN_MEMORY_REVIEW_CONFIDENCE = 0.60
MIN_EVENT_REVIEW_CONFIDENCE = 0.70

# Optional active-learning samples must be reasonably close to the person's
# configured suggestion threshold and clearly separated from the runner-up.
VOICE_TRAINING_MIN_SCORE = 0.70
VOICE_TRAINING_MIN_MARGIN = 0.10


def voice_review_lane(candidate: dict[str, Any]) -> VoiceReviewLane | None:
    """Return the user-facing review lane for a voice prototype candidate."""

    tier = candidate.get("decision_tier")
    if tier == "suggested":
        return "primary"
    if tier != "no_known_match":
        return None
    score = _number(candidate.get("best_score"))
    margin = _number(candidate.get("score_margin"))
    if score is None or score < VOICE_TRAINING_MIN_SCORE:
        return None
    if margin is not None and margin < VOICE_TRAINING_MIN_MARGIN:
        return None
    return "training"


def voice_review_disposition(candidate: dict[str, Any]) -> ReviewDisposition:
    lane = voice_review_lane(candidate)
    if lane == "primary":
        return ReviewDisposition.REVIEW_REQUIRED
    if lane == "training":
        return ReviewDisposition.OPTIONAL_LEARNING
    return ReviewDisposition.SUPPRESS


def person_memory_review_disposition(memory: dict[str, Any]) -> ReviewDisposition:
    """Only durable, evidence-backed memory deserves a user decision."""

    confidence = _number(memory.get("confidence"))
    if (
        memory.get("status") != "active"
        or memory.get("confirmation_status") != "unconfirmed"
        or memory.get("kind") not in MEANINGFUL_MEMORY_KINDS
        or confidence is None
        or confidence < MIN_MEMORY_REVIEW_CONFIDENCE
    ):
        return ReviewDisposition.SUPPRESS
    evidence_count = _integer(memory.get("evidence_count"))
    if evidence_count < 1 and not memory.get("event_id"):
        return ReviewDisposition.SUPPRESS
    return ReviewDisposition.REVIEW_REQUIRED


def event_proposal_review_disposition(
    payload: dict[str, Any], *, evidence_count: int
) -> ReviewDisposition:
    """Keep the event layer internal unless it changes trusted memory."""

    patch = payload.get("patch")
    if not isinstance(patch, dict) or evidence_count < 1:
        return ReviewDisposition.SUPPRESS
    event_kind = payload.get("event_kind")
    confidence = _number(patch.get("confidence"))
    if (
        payload.get("operation") != "create"
        or event_kind not in MEANINGFUL_EVENT_KINDS
        or confidence is None
        or confidence < MIN_EVENT_REVIEW_CONFIDENCE
    ):
        return ReviewDisposition.SUPPRESS
    if event_kind == "person_fact" and not patch.get("person_id"):
        return ReviewDisposition.SUPPRESS
    return ReviewDisposition.REVIEW_REQUIRED


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


__all__ = [
    "MEANINGFUL_EVENT_KINDS",
    "MEANINGFUL_MEMORY_KINDS",
    "MIN_EVENT_REVIEW_CONFIDENCE",
    "MIN_MEMORY_REVIEW_CONFIDENCE",
    "ReviewDisposition",
    "VOICE_TRAINING_MIN_MARGIN",
    "VOICE_TRAINING_MIN_SCORE",
    "VoiceReviewLane",
    "event_proposal_review_disposition",
    "person_memory_review_disposition",
    "voice_review_lane",
    "voice_review_disposition",
]
