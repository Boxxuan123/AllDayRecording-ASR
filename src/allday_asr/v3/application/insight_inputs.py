from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any


from .insight_projections import (
    _datetime,
    _person_ids,
    _timestamp,
    _topic_values,
    _topics,
    _unique_events,
    _unique_quotes,
    _within,
)


def observations_from_dict(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("relationship observations must be an array of objects")
    return tuple(value)


def _daily_inputs(
    all_events: tuple[dict[str, Any], ...], start: datetime, end: datetime
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    today = [
        event for event in all_events if _within(event["captured_start"], start, end)
    ]
    completed = [
        event
        for event in all_events
        if event["status"] == "completed" and _within(event["updated_at"], start, end)
    ]
    leftovers = [
        event
        for event in all_events
        if event["status"] == "active"
        and event["event_kind"] in {"task", "request", "commitment", "appointment"}
        and _timestamp(event["captured_start"]) < start
    ]
    selected = _unique_events((*today, *completed, *leftovers[-20:]))
    compact = tuple(_compact_event(event) for event in selected)
    quotes = _unique_quotes(selected)[:8]
    kinds = Counter(event["event_kind"] for event in today)
    people: dict[str, int] = defaultdict(int)
    for event in today:
        for person_id in _person_ids(event["payload"]):
            people[person_id] += 1
    objective = {
        "period": {"start": _datetime(start), "end": _datetime(end)},
        "statistics": {
            "event_count": len(today),
            "event_kinds": dict(sorted(kinds.items())),
            "decision_count": kinds["decision"],
            "new_todo_count": sum(
                event["event_kind"] in {"task", "request", "commitment", "appointment"}
                for event in today
            ),
            "completed_count": len(completed),
            "unresolved_count": len(leftovers),
            "people_interaction_count": sum(people.values()),
        },
        "important_events": [
            _compact_event(event)
            for event in today
            if event["event_kind"]
            in {"important_experience", "decision", "appointment"}
        ],
        "decisions": [
            _compact_event(event)
            for event in today
            if event["event_kind"] == "decision"
        ],
        "new_todos": [
            _compact_event(event)
            for event in today
            if event["event_kind"] in {"task", "request", "commitment", "appointment"}
        ],
        "completed": [_compact_event(event) for event in completed],
        "unresolved": [_compact_event(event) for event in leftovers[-20:]],
        "self_commitments": [
            _compact_event(event)
            for event in today
            if event["event_kind"] == "commitment"
            and event["payload"].get("commitment_direction") == "self_to_other"
        ],
        "other_commitments": [
            _compact_event(event)
            for event in today
            if event["event_kind"] == "commitment"
            and event["payload"].get("commitment_direction") == "other_to_self"
        ],
        "people_interactions": [
            {"person_id": person_id, "event_count": count}
            for person_id, count in sorted(people.items())
        ],
        "yesterday_leftovers": [_compact_event(event) for event in leftovers[-20:]],
        "key_quotes": list(quotes),
    }
    return objective, compact, quotes


def _relationship_inputs(
    person_id: str,
    all_events: tuple[dict[str, Any], ...],
    all_interactions: tuple[dict[str, Any], ...],
    previous_start: datetime,
    period_start: datetime,
    period_end: datetime,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    related = [
        event for event in all_events if person_id in _person_ids(event["payload"])
    ]
    current_events = [
        event
        for event in related
        if _within(event["captured_start"], period_start, period_end)
    ]
    previous_events = [
        event
        for event in related
        if _within(event["captured_start"], previous_start, period_start)
    ]
    outstanding = [
        event
        for event in related
        if event["status"] == "active"
        and event["event_kind"] in {"task", "request", "commitment", "appointment"}
        and _timestamp(event["captured_start"]) < period_end
    ]
    current_interactions = tuple(
        value
        for value in all_interactions
        if _within(value["start_at"], period_start, period_end)
    )
    previous_interactions = tuple(
        value
        for value in all_interactions
        if _within(value["start_at"], previous_start, period_start)
    )
    current_sessions = {value["session_id"] for value in current_interactions}
    previous_sessions = {value["session_id"] for value in previous_interactions}
    current_topics = _topics(current_events)
    previous_topics = _topics(previous_events)
    last_contact = max(
        (_timestamp(value["start_at"]) for value in all_interactions), default=None
    )
    category_counts = Counter(event["event_kind"] for event in current_events)
    facts = {
        "window": {
            "previous_start": _datetime(previous_start),
            "period_start": _datetime(period_start),
            "period_end": _datetime(period_end),
        },
        "interaction_frequency": {
            "current_session_count": len(current_sessions),
            "previous_session_count": len(previous_sessions),
            "session_delta": len(current_sessions) - len(previous_sessions),
            "current_utterance_count": len(current_interactions),
            "previous_utterance_count": len(previous_interactions),
        },
        "topics": {
            "current": current_topics,
            "previous": previous_topics,
        },
        "unfinished_commitments": [_compact_event(event) for event in outstanding],
        "event_counts": dict(sorted(category_counts.items())),
        "request_count": category_counts["request"],
        "commitment_count": category_counts["commitment"],
        "decision_count": category_counts["decision"],
        "last_contact_at": _datetime(last_contact) if last_contact else None,
        "days_since_contact": (
            max(0, (period_end - last_contact).days) if last_contact else None
        ),
        "evidence_event_ids": [
            event["event_id"]
            for event in _unique_events((*current_events, *outstanding))
        ],
        "evidence_utterance_ids": [
            value["utterance_id"] for value in current_interactions
        ],
    }
    source_events = tuple(
        _compact_event(event)
        for event in _unique_events((*current_events, *outstanding))
    )
    interactions = tuple((*previous_interactions, *current_interactions))
    return facts, source_events, interactions


def _compact_event(event: dict[str, Any]) -> dict[str, Any]:
    payload = event["payload"]
    return {
        "event_id": event["event_id"],
        "revision": event["revision"],
        "kind": event["event_kind"],
        "status": event["status"],
        "title": payload.get("title") or payload.get("summary") or "",
        "captured_at": event["captured_start"],
        "updated_at": event["updated_at"],
        "person_ids": list(_person_ids(payload)),
        "topics": list(_topic_values(payload)),
        "commitment_direction": payload.get("commitment_direction"),
        "evidence_utterance_ids": [
            value["utterance_id"] for value in event.get("evidence", ())
        ],
    }
