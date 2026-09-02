from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.person_memory import (
    PersonMemoryOperationKind,
    PersonMemorySource,
    PersonMemoryStatus,
)

from .person_memory_support import (
    _datetime,
    _event_confirmation,
    _event_memory_id,
    _event_projection,
    _event_summary,
    _event_topics,
    _projection_matches,
)


class PersonMemoryRefreshMixin:
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
