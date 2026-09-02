from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from allday_asr.v3.contracts import validate_reminder_dto
from allday_asr.v3.domain.hashing import canonical_json_sha256
from allday_asr.v3.domain.knowledge import (
    EventKind,
    EventOperationKind,
)
from allday_asr.v3.domain.reminders import (
    ReminderGenerationSubmission,
    ReminderIntent,
    ReminderOperation,
    ReminderSchedule,
)
from allday_asr.v3.ports.repositories import UnitOfWork

AUTO_APPLY_CONFIDENCE = 0.98


def _event_operation(
    intent: ReminderIntent, uow: UnitOfWork
) -> tuple[EventKind, EventOperationKind]:
    if intent.operation is ReminderOperation.CREATE_TASK:
        return EventKind.TASK, EventOperationKind.CREATE
    if intent.operation is ReminderOperation.CREATE_APPOINTMENT:
        return EventKind.APPOINTMENT, EventOperationKind.CREATE
    if intent.target_event_id is None:
        raise ValueError("reminder mutation target is required")
    current = uow.knowledge.get_event(intent.target_event_id)
    if current is None:
        raise ValueError("reminder target event does not exist")
    if current.session_id != intent.session_id:
        raise ValueError("reminder target belongs to another session")
    if current.revision != intent.expected_revision:
        raise ValueError("reminder target revision conflict")
    if current.event_kind not in {EventKind.TASK, EventKind.APPOINTMENT}:
        raise ValueError("only task and appointment events can become reminders")
    mapping = {
        ReminderOperation.UPDATE_EVENT: EventOperationKind.UPDATE,
        ReminderOperation.CANCEL_EVENT: EventOperationKind.CANCEL,
        ReminderOperation.MARK_DONE: EventOperationKind.COMPLETE,
    }
    return current.event_kind, mapping[intent.operation]


def _event_patch(intent: ReminderIntent) -> dict[str, Any]:
    if intent.operation in {
        ReminderOperation.CANCEL_EVENT,
        ReminderOperation.MARK_DONE,
    }:
        return {
            "last_reminder_signal": {
                "operation": intent.operation.value,
                "confidence": intent.confidence,
                "reason": intent.reason,
            }
        }
    patch: dict[str, Any] = {
        "actor_person_id": intent.actor_person_id,
        "commitment_direction": intent.commitment_direction.value,
        "related_person_ids": list(intent.related_person_ids),
        "confidence": intent.confidence,
        "needs_confirmation": intent.needs_confirmation,
    }
    if intent.title is not None:
        patch["title"] = intent.title.strip()
    if intent.scheduled_at is not None:
        patch["scheduled_time"] = _datetime(intent.scheduled_at)
    if intent.location is not None:
        patch["location"] = intent.location.strip()
    return patch


def _candidate_dedup_key(intent: ReminderIntent) -> str:
    if intent.operation in {
        ReminderOperation.CREATE_TASK,
        ReminderOperation.CREATE_APPOINTMENT,
    }:
        return _event_dedup_key(_event_patch(intent))
    return canonical_json_sha256(
        {
            "operation": intent.operation.value,
            "target_event_id": intent.target_event_id,
            "expected_revision": intent.expected_revision,
            "patch": _event_patch(intent),
        }
    )


def _event_dedup_key(payload: dict[str, Any]) -> str:
    title = str(payload.get("title", ""))
    return canonical_json_sha256(
        {
            "title": " ".join(title.casefold().split()),
            "actor_person_id": payload.get("actor_person_id"),
            "commitment_direction": payload.get("commitment_direction"),
            "related_person_ids": sorted(payload.get("related_person_ids", [])),
            "scheduled_time": payload.get("scheduled_time"),
            "location": (
                " ".join(str(payload["location"]).casefold().split())
                if payload.get("location")
                else None
            ),
        }
    )


def _schedule_projection(schedule: ReminderSchedule) -> dict[str, Any]:
    return validate_reminder_dto(
        {
            "event_id": schedule.event_id,
            "session_id": schedule.session_id,
            "event_revision": schedule.event_revision,
            "source_candidate_id": schedule.source_candidate_id,
            "title": schedule.title,
            "actor_person_id": schedule.actor_person_id,
            "commitment_direction": schedule.commitment_direction.value,
            "related_person_ids": list(schedule.related_person_ids),
            "scheduled_at": _datetime(schedule.scheduled_at),
            "location": schedule.location,
            "status": schedule.status.value,
            "updated_at": _datetime(schedule.updated_at),
        }
    )


def _validate_generation(
    submission: ReminderGenerationSubmission, *, allow_empty: bool = False
) -> None:
    for name, value in (
        ("producer", submission.producer),
        ("producer_version", submission.producer_version),
        ("model", submission.model),
        ("prompt_version", submission.prompt_version),
        ("extractor_version", submission.extractor_version),
    ):
        if not value.strip():
            raise ValueError(f"reminder generation {name} is required")
    if not submission.input_scope or not isinstance(submission.input_scope, dict):
        raise ValueError("reminder generation input scope is required")
    if not submission.intents and not allow_empty:
        raise ValueError("reminder generation requires at least one intent")


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= 500:
        raise ValueError("reminder page limit must be between 1 and 500")


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("reminder scheduled_at must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("reminder scheduled_at must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("reminder scheduled_at must include a timezone")
    return parsed


def _datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("reminder timestamp must be timezone-aware")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
