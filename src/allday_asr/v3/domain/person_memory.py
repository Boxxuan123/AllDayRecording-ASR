from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class PersonMemoryKind(StrEnum):
    STABLE_FACT = "stable_fact"
    PREFERENCE = "preference"
    SHORT_TERM_STATE = "short_term_state"
    PLAN = "plan"
    COMMITMENT = "commitment"
    MODEL_OBSERVATION = "model_observation"


class PersonMemoryStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    RETRACTED = "retracted"


class PersonMemorySource(StrEnum):
    HUMAN = "human"
    EVENT_PROJECTION = "event_projection"
    MODEL = "model"


class PersonMemoryConfirmation(StrEnum):
    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"
    INFERRED = "inferred"


class PersonMemoryOperationKind(StrEnum):
    CREATE = "create"
    PROJECT = "project"
    REVISE = "revise"
    EXPIRE = "expire"
    RETRACT = "retract"
    RESTORE = "restore"
    IDENTITY_REBIND = "identity_rebind"


@dataclass(frozen=True)
class PersonMemoryDraft:
    person_id: str
    kind: PersonMemoryKind
    summary: str
    valid_from: datetime
    details: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    confirmation: PersonMemoryConfirmation = PersonMemoryConfirmation.CONFIRMED
    valid_until: datetime | None = None
    event_id: str | None = None
    reminder_event_id: str | None = None
    evidence_utterance_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.person_id.strip() or not self.summary.strip():
            raise ValueError("person memory subject and summary are required")
        if not 0 <= self.confidence <= 1:
            raise ValueError("person memory confidence must be between 0 and 1")
        if self.valid_from.tzinfo is None or self.valid_from.utcoffset() is None:
            raise ValueError("person memory valid_from requires a timezone")
        if self.valid_until is not None:
            if (
                self.valid_until.tzinfo is None
                or self.valid_until.utcoffset() is None
            ):
                raise ValueError("person memory valid_until requires a timezone")
            if self.valid_until <= self.valid_from:
                raise ValueError("person memory validity range must be positive")
        if self.kind in {
            PersonMemoryKind.SHORT_TERM_STATE,
            PersonMemoryKind.PLAN,
        } and self.valid_until is None:
            raise ValueError("short-term state and plan memories require valid_until")
        if len(set(self.evidence_utterance_ids)) != len(
            self.evidence_utterance_ids
        ):
            raise ValueError("person memory evidence must be unique")
        if self.event_id is None and not self.evidence_utterance_ids:
            raise ValueError("person memory requires event or utterance evidence")


__all__ = [
    "PersonMemoryConfirmation",
    "PersonMemoryDraft",
    "PersonMemoryKind",
    "PersonMemoryOperationKind",
    "PersonMemorySource",
    "PersonMemoryStatus",
]
