from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

from allday_asr.v3.domain.person_memory import (
    PersonMemoryConfirmation,
    PersonMemoryDraft,
    PersonMemoryKind,
    PersonMemoryStatus,
)


def _event_projection(person_id: str, event: dict[str, Any]) -> dict[str, Any] | None:
    event_kind = str(event["event_kind"])
    kind = {
        "person_fact": PersonMemoryKind.STABLE_FACT,
        "commitment": PersonMemoryKind.COMMITMENT,
        "request": PersonMemoryKind.COMMITMENT,
        "task": PersonMemoryKind.PLAN,
        "appointment": PersonMemoryKind.PLAN,
    }.get(event_kind)
    if kind is None:
        return None
    payload = dict(event["payload"])
    if (
        event_kind == "person_fact"
        and payload.get("actor_person_id", payload.get("person_id")) != person_id
    ):
        return None
    if event_kind in {"task", "request"} and payload.get("commitment_direction") in {
        "self_to_other",
        "other_to_self",
        "mutual",
    }:
        kind = PersonMemoryKind.COMMITMENT
    summary = _event_summary(payload, event_kind)
    start_value = payload.get("starts_at") or payload.get("scheduled_at")
    if kind is PersonMemoryKind.PLAN:
        start_value = start_value or payload.get("scheduled_time")
    valid_from = _parse_datetime(
        start_value or event.get("captured_start") or event["created_at"]
    )
    valid_until: datetime | None = None
    if kind is PersonMemoryKind.PLAN:
        scheduled = _optional_datetime(
            payload.get("ends_at")
            or payload.get("scheduled_at")
            or payload.get("scheduled_time")
        )
        if scheduled is None or scheduled <= valid_from:
            scheduled = valid_from + timedelta(days=30)
        valid_until = scheduled
    elif payload.get("due_at"):
        valid_until = _optional_datetime(payload["due_at"])
    confirmation = _event_confirmation(event.get("actor"))
    details = {
        "event_kind": event_kind,
        "event_revision": int(event["revision"]),
        "event_status": event["status"],
        "commitment_direction": payload.get("commitment_direction"),
        "actor_person_id": payload.get("actor_person_id"),
        "related_person_ids": payload.get("related_person_ids", []),
        "topics": _event_topics(payload),
        "scheduled_time": payload.get("scheduled_time"),
        "person_role": (
            "actor" if payload.get("actor_person_id") == person_id else "related"
        ),
    }
    return {
        "kind": kind.value,
        "summary": summary,
        "details": details,
        "confidence": float(payload.get("confidence", 0.9)),
        "confirmation_status": confirmation.value,
        "valid_from": _datetime(valid_from),
        "valid_until": _datetime(valid_until) if valid_until is not None else None,
        "status": (
            PersonMemoryStatus.ACTIVE.value
            if event["status"] == "active"
            else PersonMemoryStatus.EXPIRED.value
        ),
    }


def _projection_matches(current: dict[str, Any], projection: dict[str, Any]) -> bool:
    keys = (
        "kind",
        "summary",
        "details",
        "confidence",
        "confirmation_status",
        "valid_from",
        "valid_until",
        "status",
    )
    return all(current.get(key) == projection.get(key) for key in keys)


def _event_summary(payload: dict[str, Any], fallback: str) -> str:
    for key in ("summary", "content", "text", "description", "title"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def _event_confirmation(value: object) -> PersonMemoryConfirmation:
    actor = str(value or "").strip()
    if actor.startswith("system:"):
        return PersonMemoryConfirmation.UNCONFIRMED
    return PersonMemoryConfirmation.CONFIRMED


def _event_topics(payload: dict[str, Any]) -> list[str]:
    values = payload.get("topics", [])
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        topic = value.strip()
        if not topic or topic.startswith(("陈述 ·", "行动候选 ·")):
            continue
        result.append(topic)
    return list(dict.fromkeys(result))


def _event_memory_id(person_id: str, event_id: str) -> str:
    digest = hashlib.sha256(f"{person_id}:{event_id}".encode()).hexdigest()[:24]
    return f"pmem-{digest}"


def _parse_datetime(value: object) -> datetime:
    parsed = _optional_datetime(value)
    if parsed is None:
        raise ValueError("person memory timestamp is required")
    return parsed


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("person memory timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("person memory timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("person memory timestamp requires a timezone")
    return parsed


def _datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("person memory timestamps require timezone information")
    return value.isoformat()


def memory_draft_from_dict(person_id: str, value: dict[str, Any]) -> PersonMemoryDraft:
    allowed = {
        "kind",
        "summary",
        "details",
        "confidence",
        "valid_from",
        "valid_until",
        "event_id",
        "reminder_event_id",
        "evidence_utterance_ids",
    }
    if set(value) - allowed or not {"kind", "summary", "valid_from"} <= set(value):
        raise ValueError("person memory fields are invalid")
    if not isinstance(value["kind"], str) or not isinstance(value["summary"], str):
        raise ValueError("person memory kind and summary are invalid")
    details = value.get("details", {})
    evidence = value.get("evidence_utterance_ids", [])
    if (
        not isinstance(details, dict)
        or not isinstance(evidence, list)
        or not all(isinstance(item, str) for item in evidence)
    ):
        raise ValueError("person memory details or evidence are invalid")
    confidence = value.get("confidence", 1.0)
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("person memory confidence is invalid")
    for field in ("event_id", "reminder_event_id"):
        if value.get(field) is not None and not isinstance(value[field], str):
            raise ValueError(f"person memory {field} is invalid")
    return PersonMemoryDraft(
        person_id=person_id,
        kind=PersonMemoryKind(value["kind"]),
        summary=value["summary"],
        details=details,
        confidence=float(confidence),
        valid_from=_parse_datetime(value["valid_from"]),
        valid_until=_optional_datetime(value.get("valid_until")),
        event_id=value.get("event_id"),
        reminder_event_id=value.get("reminder_event_id"),
        evidence_utterance_ids=tuple(evidence),
    )


def revision_from_dict(value: dict[str, Any]) -> dict[str, Any]:
    required = {"summary", "details", "confidence", "valid_from"}
    allowed = required | {"valid_until", "reminder_event_id"}
    if set(value) != allowed and not (required <= set(value) <= allowed):
        raise ValueError("person memory revision fields are invalid")
    if not isinstance(value["details"], dict):
        raise ValueError("person memory revision details are invalid")
    if not isinstance(value["summary"], str):
        raise ValueError("person memory revision summary is invalid")
    confidence = value["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("person memory revision confidence is invalid")
    reminder = value.get("reminder_event_id")
    if reminder is not None and not isinstance(reminder, str):
        raise ValueError("person memory reminder_event_id is invalid")
    return {
        "summary": value["summary"],
        "details": dict(value["details"]),
        "confidence": float(confidence),
        "valid_from": _parse_datetime(value["valid_from"]),
        "valid_until": _optional_datetime(value.get("valid_until")),
        "reminder_event_id": reminder,
    }
