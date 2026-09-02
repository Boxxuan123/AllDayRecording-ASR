from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.person_memory import (
    PersonMemoryConfirmation,
    PersonMemoryDraft,
    PersonMemoryKind,
    PersonMemoryOperationKind,
    PersonMemorySource,
    PersonMemoryStatus,
)
from allday_asr.v3.ports.repositories import UnitOfWork


UnitOfWorkFactory = Callable[[], UnitOfWork]
DateTimeClock = Callable[[], datetime]


class PersonMemoryService:
    """Versioned, evidence-linked memory for stable people across sessions."""

    def __init__(
        self, uow_factory: UnitOfWorkFactory, *, now: DateTimeClock | None = None
    ) -> None:
        self._uow_factory = uow_factory
        self._now = now or (lambda: datetime.now(timezone.utc))

    def person(self, person_id: str, limit: int = 200) -> dict[str, Any]:
        if not 1 <= limit <= 500:
            raise ValueError("person memory limit must be between 1 and 500")
        with self._uow_factory() as uow:
            return uow.person_memories.person_detail(person_id, limit)

    def summaries(self) -> dict[str, dict[str, Any]]:
        with self._uow_factory() as uow:
            return uow.person_memories.summary_counts()

    def update_profile(
        self,
        person_id: str,
        *,
        display_name: str,
        aliases: Sequence[str],
        relationship_labels: Sequence[str],
        notes: str,
        actor: str = "desktop-user",
    ) -> dict[str, Any]:
        name = display_name.strip()
        if not name or not actor.strip():
            raise ValueError("person profile name and actor are required")
        with self._uow_factory() as uow:
            return uow.person_memories.update_profile(
                person_id,
                name,
                tuple(aliases),
                tuple(relationship_labels),
                notes,
                actor,
                _datetime(self._now()),
            )

    def refresh(
        self, person_id: str, actor: str = "system:v35-projection"
    ) -> dict[str, Any]:
        with self._uow_factory() as uow:
            events = uow.person_memories.event_sources(person_id)
        created = 0
        revised = 0
        retracted = 0
        skipped = 0
        for event in events:
            projection = _event_projection(person_id, event)
            memory_id = _event_memory_id(person_id, str(event["event_id"]))
            with self._uow_factory() as uow:
                try:
                    current = uow.person_memories.current_memory(memory_id)
                except KeyError:
                    current = None
                # A human revision owns the memory from this point onward. Event
                # refreshes must never silently replace the user's correction.
                if (
                    current is not None
                    and current["source"] != PersonMemorySource.EVENT_PROJECTION.value
                ):
                    skipped += 1
                    continue
                if projection is None:
                    if current is None:
                        skipped += 1
                        continue
                    payload = dict(event["payload"])
                    obsolete_details = dict(current["details"])
                    obsolete_details.update(
                        {
                            "event_revision": int(event["revision"]),
                            "event_status": event["status"],
                            "actor_person_id": payload.get("actor_person_id"),
                            "related_person_ids": payload.get("related_person_ids", []),
                            "topics": _event_topics(payload),
                            "person_role": "related",
                        }
                    )
                    obsolete = {
                        "kind": str(current["kind"]),
                        "summary": _event_summary(payload, str(event["event_kind"])),
                        "details": obsolete_details,
                        "confidence": float(
                            payload.get("confidence", current["confidence"])
                        ),
                        "confirmation_status": _event_confirmation(
                            event.get("actor")
                        ).value,
                        "valid_from": str(current["valid_from"]),
                        "valid_until": current["valid_until"],
                        "status": PersonMemoryStatus.RETRACTED.value,
                    }
                    if _projection_matches(current, obsolete):
                        skipped += 1
                        continue
                    evidence_ids = tuple(
                        dict.fromkeys(
                            str(item["utterance_id"])
                            for item in current["evidence"]
                            if item.get("utterance_id") is not None
                        )
                    )
                    uow.person_memories.add_revision(
                        memory_id=memory_id,
                        person_id=person_id,
                        kind=obsolete["kind"],
                        summary=obsolete["summary"],
                        details=obsolete["details"],
                        source=PersonMemorySource.EVENT_PROJECTION.value,
                        confidence=obsolete["confidence"],
                        confirmation_status=obsolete["confirmation_status"],
                        valid_from=obsolete["valid_from"],
                        valid_until=obsolete["valid_until"],
                        status=obsolete["status"],
                        event_id=str(event["event_id"]),
                        reminder_event_id=current["reminder_event_id"],
                        evidence_utterance_ids=evidence_ids,
                        operation_id=new_ulid(),
                        operation_kind=PersonMemoryOperationKind.PROJECT.value,
                        actor=actor,
                        created_at=_datetime(self._now()),
                        operation_payload={
                            "event_id": event["event_id"],
                            "event_revision": event["revision"],
                            "reason": "person_is_related_but_not_the_fact_subject",
                        },
                    )
                    if current["status"] == PersonMemoryStatus.RETRACTED.value:
                        revised += 1
                    else:
                        retracted += 1
                    continue
                if current is not None and _projection_matches(current, projection):
                    skipped += 1
                    continue
                uow.person_memories.add_revision(
                    memory_id=memory_id,
                    person_id=person_id,
                    kind=projection["kind"],
                    summary=projection["summary"],
                    details=projection["details"],
                    source=PersonMemorySource.EVENT_PROJECTION.value,
                    confidence=projection["confidence"],
                    confirmation_status=projection["confirmation_status"],
                    valid_from=projection["valid_from"],
                    valid_until=projection["valid_until"],
                    status=projection["status"],
                    event_id=str(event["event_id"]),
                    reminder_event_id=(
                        str(event["event_id"])
                        if event.get("reminder_status") is not None
                        else None
                    ),
                    evidence_utterance_ids=event["evidence_utterance_ids"],
                    operation_id=new_ulid(),
                    operation_kind=PersonMemoryOperationKind.PROJECT.value,
                    actor=actor,
                    created_at=_datetime(self._now()),
                    operation_payload={
                        "event_id": event["event_id"],
                        "event_revision": event["revision"],
                    },
                )
            if current is None:
                created += 1
            else:
                revised += 1
        expired = self._expire_due(person_id, actor)
        return {
            "person_id": person_id,
            "created_count": created,
            "revised_count": revised,
            "retracted_count": retracted,
            "expired_count": expired,
            "skipped_count": skipped,
        }

    def create(
        self,
        draft: PersonMemoryDraft,
        *,
        actor: str = "desktop-user",
    ) -> dict[str, Any]:
        source = (
            PersonMemorySource.MODEL
            if draft.kind is PersonMemoryKind.MODEL_OBSERVATION
            else PersonMemorySource.HUMAN
        )
        confirmation = (
            PersonMemoryConfirmation.INFERRED
            if source is PersonMemorySource.MODEL
            else draft.confirmation
        )
        with self._uow_factory() as uow:
            return uow.person_memories.add_revision(
                memory_id=new_ulid(),
                person_id=draft.person_id,
                kind=draft.kind.value,
                summary=draft.summary,
                details=draft.details,
                source=source.value,
                confidence=draft.confidence,
                confirmation_status=confirmation.value,
                valid_from=_datetime(draft.valid_from),
                valid_until=(
                    _datetime(draft.valid_until)
                    if draft.valid_until is not None
                    else None
                ),
                status=PersonMemoryStatus.ACTIVE.value,
                event_id=draft.event_id,
                reminder_event_id=draft.reminder_event_id,
                evidence_utterance_ids=draft.evidence_utterance_ids,
                operation_id=new_ulid(),
                operation_kind=PersonMemoryOperationKind.CREATE.value,
                actor=actor,
                created_at=_datetime(self._now()),
                operation_payload={"source": source.value},
            )

    def revise(
        self,
        memory_id: str,
        *,
        summary: str,
        details: dict[str, Any],
        confidence: float,
        valid_from: datetime,
        valid_until: datetime | None,
        reminder_event_id: str | None,
        actor: str = "desktop-user",
    ) -> dict[str, Any]:
        with self._uow_factory() as uow:
            current = uow.person_memories.current_memory(memory_id)
        evidence_ids = tuple(
            str(item["utterance_id"])
            for item in current["evidence"]
            if item.get("utterance_id") is not None
        )
        draft = PersonMemoryDraft(
            person_id=str(current["person_id"]),
            kind=PersonMemoryKind(str(current["kind"])),
            summary=summary,
            details=details,
            confidence=confidence,
            confirmation=PersonMemoryConfirmation.CONFIRMED,
            valid_from=valid_from,
            valid_until=valid_until,
            event_id=current["event_id"],
            reminder_event_id=reminder_event_id,
            evidence_utterance_ids=evidence_ids,
        )
        with self._uow_factory() as uow:
            return uow.person_memories.add_revision(
                memory_id=memory_id,
                person_id=draft.person_id,
                kind=draft.kind.value,
                summary=draft.summary,
                details=draft.details,
                source=PersonMemorySource.HUMAN.value,
                confidence=draft.confidence,
                confirmation_status=PersonMemoryConfirmation.CONFIRMED.value,
                valid_from=_datetime(draft.valid_from),
                valid_until=(
                    _datetime(draft.valid_until)
                    if draft.valid_until is not None
                    else None
                ),
                status=PersonMemoryStatus.ACTIVE.value,
                event_id=draft.event_id,
                reminder_event_id=draft.reminder_event_id,
                evidence_utterance_ids=draft.evidence_utterance_ids,
                operation_id=new_ulid(),
                operation_kind=PersonMemoryOperationKind.REVISE.value,
                actor=actor,
                created_at=_datetime(self._now()),
                operation_payload={"previous_revision": current["revision"]},
            )

    def expire(self, memory_id: str, actor: str = "desktop-user") -> dict[str, Any]:
        return self._status(
            memory_id,
            PersonMemoryStatus.EXPIRED,
            PersonMemoryOperationKind.EXPIRE,
            actor,
        )

    def retract(self, memory_id: str, actor: str = "desktop-user") -> dict[str, Any]:
        return self._status(
            memory_id,
            PersonMemoryStatus.RETRACTED,
            PersonMemoryOperationKind.RETRACT,
            actor,
        )

    def undo(self, memory_id: str, actor: str = "desktop-user") -> dict[str, Any]:
        with self._uow_factory() as uow:
            return uow.person_memories.undo(
                memory_id, actor, new_ulid(), _datetime(self._now())
            )

    def _status(
        self,
        memory_id: str,
        status: PersonMemoryStatus,
        operation: PersonMemoryOperationKind,
        actor: str,
    ) -> dict[str, Any]:
        with self._uow_factory() as uow:
            return uow.person_memories.revise_status(
                memory_id,
                status.value,
                actor,
                new_ulid(),
                operation.value,
                _datetime(self._now()),
            )

    def _expire_due(self, person_id: str, actor: str) -> int:
        with self._uow_factory() as uow:
            active = uow.person_memories.list_current(
                person_id, limit=500, include_inactive=False
            )
        now = self._now()
        due = [
            item
            for item in active
            if item["valid_until"] is not None
            and datetime.fromisoformat(str(item["valid_until"])) <= now
        ]
        for item in due:
            self._status(
                str(item["memory_id"]),
                PersonMemoryStatus.EXPIRED,
                PersonMemoryOperationKind.EXPIRE,
                actor,
            )
        return len(due)


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
    if actor.startswith("system:") or actor == "legacy_v2_import":
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


__all__ = [
    "PersonMemoryService",
    "memory_draft_from_dict",
    "revision_from_dict",
]
