from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from allday_asr.v3.ports.insight_generation import (
    InsightModelGenerator,
    InsightReasoningEffort,
)

from .insight_errors import InsightGenerationFailed


def _unique_quotes(events: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for event in events:
        for value in event.get("evidence", ()):
            utterance_id = str(value["utterance_id"])
            if utterance_id in seen:
                continue
            seen.add(utterance_id)
            result.append(
                {
                    "utterance_id": utterance_id,
                    "text": value["text"],
                    "start_at": value["start_at"],
                    "event_id": event["event_id"],
                }
            )
    return tuple(result)


def _unique_events(events: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for event in events:
        event_id = str(event["event_id"])
        if event_id in seen:
            continue
        seen.add(event_id)
        result.append(event)
    return tuple(result)


def _person_ids(payload: dict[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    actor = payload.get("actor_person_id")
    if isinstance(actor, str) and actor:
        values.append(actor)
    for key in ("related_person_ids", "person_ids"):
        raw = payload.get(key, ())
        if isinstance(raw, list):
            values.extend(value for value in raw if isinstance(value, str) and value)
    person_id = payload.get("person_id")
    if isinstance(person_id, str) and person_id:
        values.append(person_id)
    return tuple(dict.fromkeys(values))


def _topic_values(payload: dict[str, Any]) -> tuple[str, ...]:
    raw = payload.get("topics", ())
    if not isinstance(raw, list):
        return ()
    return tuple(
        dict.fromkeys(
            value.strip() for value in raw if isinstance(value, str) and value.strip()
        )
    )


def _topics(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for event in events:
        counts.update(_topic_values(event["payload"]))
    return [
        {"label": label, "count": count}
        for label, count in sorted(
            counts.items(), key=lambda item: (-item[1], item[0])
        )[:10]
    ]


def _provenance(
    generator: InsightModelGenerator, result: object, kind: str
) -> dict[str, Any]:
    effort = result.reasoning_effort
    return {
        "kind": kind,
        "producer": "codex-insights",
        "producer_version": generator.producer_version,
        "model": generator.model_label,
        "prompt_version": generator.prompt_version,
        "extractor_version": generator.extractor_version,
        "turn_id": str(result.turn_id),
        "reasoning_effort": effort.value,
        "usage": dict(result.usage),
    }


def _effort(
    requested: str | None, complexity: int, *, relationship: bool = False
) -> InsightReasoningEffort:
    if requested not in {None, "", "auto"}:
        try:
            return InsightReasoningEffort(requested)
        except ValueError as exc:
            raise ValueError("insight reasoning_effort is invalid") from exc
    if complexity <= (4 if relationship else 6):
        return InsightReasoningEffort.LOW
    if complexity <= (30 if relationship else 24):
        return InsightReasoningEffort.MEDIUM
    if complexity <= 100:
        return InsightReasoningEffort.HIGH
    return InsightReasoningEffort.XHIGH


def _string_ids(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise InsightGenerationFailed(f"Codex {label} evidence ids are invalid")
    return tuple(dict.fromkeys(value))


def _ensure_subset(values: tuple[str, ...], allowed: set[str], label: str) -> None:
    unknown = set(values) - allowed
    if unknown:
        raise InsightGenerationFailed(
            f"Codex invented or referenced unavailable {label} evidence"
        )


def _date(value: str | date) -> date:
    if isinstance(value, datetime):
        raise ValueError("insight date must not include a time")
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError("insight date is invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("insight date is invalid") from exc
    if parsed.isoformat() != value:
        raise ValueError("insight date must use YYYY-MM-DD")
    return parsed


def _period(day: date, timezone_name: str) -> tuple[datetime, datetime]:
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("insight timezone is invalid") from exc
    local_start = datetime.combine(day, time.min, tzinfo=zone)
    local_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def _within(value: object, start: datetime, end: datetime) -> bool:
    timestamp = _timestamp(value)
    return start <= timestamp < end


def _timestamp(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("stored insight source timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stored insight source timestamp requires a timezone")
    return parsed.astimezone(timezone.utc)


def _datetime(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
