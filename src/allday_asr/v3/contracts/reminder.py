from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any, TypedDict, cast


_STABLE_ID = re.compile(
    r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}|[0-9A-HJKMNP-TV-Z]{26})$"
)
_FIELDS = frozenset(
    {
        "event_id",
        "session_id",
        "event_revision",
        "source_candidate_id",
        "title",
        "actor_person_id",
        "commitment_direction",
        "related_person_ids",
        "scheduled_at",
        "location",
        "status",
        "updated_at",
    }
)
_DIRECTIONS = {"self_to_other", "other_to_self", "mutual", "not_applicable"}
_STATUSES = {"scheduled", "delivered", "completed", "cancelled", "stale"}


class ReminderDto(TypedDict):
    event_id: str
    session_id: str
    event_revision: int
    source_candidate_id: str
    title: str
    actor_person_id: str
    commitment_direction: str
    related_person_ids: list[str]
    scheduled_at: str
    location: str | None
    status: str
    updated_at: str


def validate_reminder_dto(payload: Mapping[str, Any]) -> ReminderDto:
    value = dict(payload)
    if set(value) != _FIELDS:
        raise ValueError("reminder DTO fields do not match schema")
    for field in ("event_id", "session_id", "source_candidate_id"):
        item = value[field]
        if not isinstance(item, str) or _STABLE_ID.fullmatch(item) is None:
            raise ValueError(f"reminder DTO {field} violates schema")
    revision = value["event_revision"]
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ValueError("reminder DTO event_revision violates schema")
    for field in ("title", "actor_person_id"):
        item = value[field]
        if not isinstance(item, str) or not item:
            raise ValueError(f"reminder DTO {field} violates schema")
    if value["commitment_direction"] not in _DIRECTIONS:
        raise ValueError("reminder DTO commitment_direction violates schema")
    related = value["related_person_ids"]
    if (
        not isinstance(related, list)
        or any(not isinstance(item, str) or not item for item in related)
        or len(set(related)) != len(related)
    ):
        raise ValueError("reminder DTO related_person_ids violates schema")
    location = value["location"]
    if location is not None and not isinstance(location, str):
        raise ValueError("reminder DTO location violates schema")
    if value["status"] not in _STATUSES:
        raise ValueError("reminder DTO status violates schema")
    scheduled_at = _datetime(value["scheduled_at"], "scheduled_at")
    updated_at = _datetime(value["updated_at"], "updated_at")
    value["scheduled_at"] = scheduled_at
    value["updated_at"] = updated_at
    return cast(ReminderDto, value)


def _datetime(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"reminder DTO {field} violates schema")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"reminder DTO {field} violates schema") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"reminder DTO {field} violates schema")
    return value


__all__ = ["ReminderDto", "validate_reminder_dto"]
