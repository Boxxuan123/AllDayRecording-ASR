from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class ReminderOperation(StrEnum):
    CREATE_TASK = "CREATE_TASK"
    CREATE_APPOINTMENT = "CREATE_APPOINTMENT"
    UPDATE_EVENT = "UPDATE_EVENT"
    CANCEL_EVENT = "CANCEL_EVENT"
    MARK_DONE = "MARK_DONE"
    IGNORE = "IGNORE"


class CommitmentDirection(StrEnum):
    SELF_TO_OTHER = "self_to_other"
    OTHER_TO_SELF = "other_to_self"
    MUTUAL = "mutual"
    NOT_APPLICABLE = "not_applicable"


class ReminderCandidateStatus(StrEnum):
    PENDING_CONFIRMATION = "pending_confirmation"
    AUTO_APPLIED = "auto_applied"
    CONFIRMED = "confirmed"
    MODIFIED = "modified"
    IGNORED = "ignored"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"


class ReminderScheduleStatus(StrEnum):
    SCHEDULED = "scheduled"
    DELIVERED = "delivered"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    STALE = "stale"


class ReminderFeedbackAction(StrEnum):
    AUTO_APPLY = "auto_apply"
    CONFIRM = "confirm"
    MODIFY = "modify"
    IGNORE = "ignore"
    DEDUPLICATE = "deduplicate"
    CONFLICT = "conflict"
    DELIVER = "deliver"


@dataclass(frozen=True)
class ReminderIntent:
    operation: ReminderOperation
    session_id: str
    actor_person_id: str
    commitment_direction: CommitmentDirection
    confidence: float
    evidence_utterance_ids: tuple[str, ...]
    needs_confirmation: bool
    title: str | None = None
    related_person_ids: tuple[str, ...] = field(default_factory=tuple)
    scheduled_at: datetime | None = None
    location: str | None = None
    target_event_id: str | None = None
    expected_revision: int = 0
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.session_id.strip() or not self.actor_person_id.strip():
            raise ValueError("reminder session and actor are required")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("reminder confidence must be between zero and one")
        if not self.evidence_utterance_ids or any(
            not value.strip() for value in self.evidence_utterance_ids
        ):
            raise ValueError("reminder evidence is required")
        if len(set(self.evidence_utterance_ids)) != len(
            self.evidence_utterance_ids
        ):
            raise ValueError("reminder evidence ids must be unique")
        if len(set(self.related_person_ids)) != len(self.related_person_ids):
            raise ValueError("related person ids must be unique")
        if self.scheduled_at is not None and (
            self.scheduled_at.tzinfo is None
            or self.scheduled_at.utcoffset() is None
        ):
            raise ValueError("reminder schedule must be timezone-aware")
        if self.operation in {
            ReminderOperation.CREATE_TASK,
            ReminderOperation.CREATE_APPOINTMENT,
        }:
            if not self.title or not self.title.strip() or self.scheduled_at is None:
                raise ValueError("reminder create requires title and scheduled time")
            if self.target_event_id is not None or self.expected_revision != 0:
                raise ValueError("reminder create cannot target an existing event")
        elif self.operation in {
            ReminderOperation.UPDATE_EVENT,
            ReminderOperation.CANCEL_EVENT,
            ReminderOperation.MARK_DONE,
        }:
            if not self.target_event_id or self.expected_revision < 1:
                raise ValueError("reminder mutation requires event and revision")
            if self.operation is ReminderOperation.UPDATE_EVENT and not any(
                value is not None for value in (self.title, self.scheduled_at, self.location)
            ):
                raise ValueError("reminder update requires an editable field")
        elif self.operation is ReminderOperation.IGNORE:
            if not self.reason or not self.reason.strip():
                raise ValueError("ignored reminder intent requires a reason")
            if self.target_event_id is not None or self.expected_revision != 0:
                raise ValueError("ignored reminder intent cannot mutate an event")


@dataclass(frozen=True)
class ReminderGenerationSubmission:
    producer: str
    producer_version: str
    model: str
    prompt_version: str
    extractor_version: str
    input_scope: dict[str, Any]
    intents: tuple[ReminderIntent, ...]


@dataclass(frozen=True)
class ReminderCandidate:
    candidate_id: str
    proposal_id: str
    generation_id: str
    operation: ReminderOperation
    session_id: str
    title: str | None
    actor_person_id: str
    commitment_direction: CommitmentDirection
    related_person_ids: tuple[str, ...]
    scheduled_at: datetime | None
    location: str | None
    confidence: float
    needs_confirmation: bool
    target_event_id: str | None
    expected_revision: int
    dedup_key: str
    status: ReminderCandidateStatus
    created_at: datetime
    matched_event_id: str | None = None
    conflict_reason: str | None = None
    resolved_at: datetime | None = None
    resolved_by: str | None = None


@dataclass(frozen=True)
class ReminderSchedule:
    event_id: str
    session_id: str
    event_revision: int
    source_candidate_id: str
    title: str
    actor_person_id: str
    commitment_direction: CommitmentDirection
    related_person_ids: tuple[str, ...]
    scheduled_at: datetime
    location: str | None
    status: ReminderScheduleStatus
    dedup_key: str
    created_at: datetime
    updated_at: datetime
    delivered_at: datetime | None = None


@dataclass(frozen=True)
class ReminderFeedback:
    feedback_id: str
    candidate_id: str
    action: ReminderFeedbackAction
    actor: str
    details: dict[str, Any]
    created_at: datetime


__all__ = [
    "CommitmentDirection",
    "ReminderCandidate",
    "ReminderCandidateStatus",
    "ReminderFeedback",
    "ReminderFeedbackAction",
    "ReminderGenerationSubmission",
    "ReminderIntent",
    "ReminderOperation",
    "ReminderSchedule",
    "ReminderScheduleStatus",
]
