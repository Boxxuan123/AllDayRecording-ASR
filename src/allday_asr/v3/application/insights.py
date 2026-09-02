from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from allday_asr.v3.domain.hashing import canonical_json_sha256
from allday_asr.v3.domain.ids import new_ulid, stable_ulid
from allday_asr.v3.domain.insights import (
    DAILY_NARRATIVE_SECTIONS,
    ModelObservation,
    NarrativeItem,
)
from allday_asr.v3.domain.knowledge import (
    DerivationDependency,
    GenerationRecord,
    GenerationStatus,
    KnowledgeLayer,
)
from allday_asr.v3.ports.insight_generation import (
    DailyInsightModelRequest,
    InsightModelGenerator,
    InsightReasoningEffort,
    RelationshipInsightModelRequest,
)
from allday_asr.v3.ports.repositories import UnitOfWork


UnitOfWorkFactory = Callable[[], UnitOfWork]
DateTimeClock = Callable[[], datetime]


class InsightGenerationUnavailable(RuntimeError):
    pass


class InsightGenerationFailed(RuntimeError):
    pass


class DailyInsightService:
    """Builds grounded V3.6 summaries without ever reading an older summary."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        generator: InsightModelGenerator | None,
        *,
        now: DateTimeClock | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._generator = generator
        self._now = now or _utc_now

    def generate_daily(
        self,
        summary_date: str | date,
        timezone_name: str,
        *,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        generator = self._require_generator()
        day = _date(summary_date)
        start, end = _period(day, timezone_name)
        with self._uow_factory() as uow:
            all_events = uow.insights.event_sources()
        objective, source_events, quotes = _daily_inputs(all_events, start, end)
        request = DailyInsightModelRequest(
            summary_date=day.isoformat(),
            timezone=timezone_name,
            objective=objective,
            source_events=source_events,
            key_quotes=quotes,
        )
        effort = _effort(reasoning_effort, len(source_events) + len(quotes))
        try:
            result = generator.generate_daily(request, effort)
        except Exception as exc:
            raise InsightGenerationFailed(
                "Codex daily summary generation failed"
            ) from exc
        allowed_events, allowed_utterances = _allowed_evidence(source_events, quotes)
        narrative = _validated_narrative(
            result.narrative, allowed_events, allowed_utterances
        )
        input_document = {
            "summary_date": day.isoformat(),
            "timezone": timezone_name,
            "objective": objective,
            "source_events": source_events,
            "key_quotes": quotes,
        }
        input_sha256 = canonical_json_sha256(input_document)
        summary_id = stable_ulid("daily-summary", day.isoformat(), timezone_name)
        now = self._now()
        provenance = _provenance(generator, result, "daily_summary")
        with self._uow_factory() as uow:
            generation = _add_generation(
                uow,
                generator,
                input_sha256,
                {
                    "kind": "daily_summary",
                    "summary_date": day.isoformat(),
                    "timezone": timezone_name,
                },
                now,
            )
            revision = uow.insights.next_daily_revision(summary_id)
            _assert_evidence_current(uow, allowed_events, allowed_utterances)
            uow.insights.add_daily(
                summary_id=summary_id,
                revision=revision,
                summary_date=day.isoformat(),
                timezone=timezone_name,
                period_start=_datetime(start),
                period_end=_datetime(end),
                objective=objective,
                narrative=narrative,
                input_sha256=input_sha256,
                generation_id=generation.generation_id,
                provenance=provenance,
                status="active",
                created_by="model:codex",
                created_at=_datetime(now),
            )
            _link_evidence(
                uow,
                "daily_summary",
                summary_id,
                revision,
                allowed_events,
                allowed_utterances,
                now,
            )
            uow.insights.add_operation(
                new_ulid(),
                "daily_summary",
                summary_id,
                revision,
                "generate",
                "model:codex",
                {"input_sha256": input_sha256},
                _datetime(now),
            )
            uow.audit.append(
                "insight.daily.generated",
                "model:codex",
                "daily_summary",
                summary_id,
                {"revision": revision, "input_sha256": input_sha256},
            )
            return uow.insights.daily(summary_id)

    def daily(self, summary_date: str | date, timezone_name: str) -> dict[str, Any]:
        summary_id = stable_ulid(
            "daily-summary", _date(summary_date).isoformat(), timezone_name
        )
        with self._uow_factory() as uow:
            return uow.insights.daily(summary_id)

    def list_daily(self, limit: int = 31) -> tuple[dict[str, Any], ...]:
        if not 1 <= limit <= 366:
            raise ValueError("daily summary limit must be between 1 and 366")
        with self._uow_factory() as uow:
            return uow.insights.list_daily(limit)

    def generate_relationship(
        self,
        person_id: str,
        window_days: int,
        end_date: str | date,
        timezone_name: str,
        *,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        if window_days not in {7, 30}:
            raise ValueError("relationship observation window must be 7 or 30 days")
        generator = self._require_generator()
        day = _date(end_date)
        _unused, period_end = _period(day, timezone_name)
        period_start = period_end - timedelta(days=window_days)
        previous_start = period_start - timedelta(days=window_days)
        with self._uow_factory() as uow:
            person = uow.insights.person_profile(person_id)
            all_events = uow.insights.event_sources()
            all_interactions = uow.insights.person_interactions(person_id)
        facts, source_events, interactions = _relationship_inputs(
            person_id,
            all_events,
            all_interactions,
            previous_start,
            period_start,
            period_end,
        )
        request = RelationshipInsightModelRequest(
            person=person,
            window_days=window_days,
            end_date=day.isoformat(),
            timezone=timezone_name,
            verified_facts=facts,
            source_events=source_events,
            interactions=interactions,
        )
        effort = _effort(
            reasoning_effort, len(source_events) + len(interactions), relationship=True
        )
        try:
            result = generator.generate_relationship(request, effort)
        except Exception as exc:
            raise InsightGenerationFailed(
                "Codex relationship observation generation failed"
            ) from exc
        allowed_events, allowed_utterances = _allowed_evidence(
            source_events, interactions
        )
        observations = _validated_observations(
            result.observations, allowed_events, allowed_utterances
        )
        input_document = {
            "person": person,
            "window_days": window_days,
            "end_date": day.isoformat(),
            "timezone": timezone_name,
            "verified_facts": facts,
            "source_events": source_events,
            "interactions": interactions,
        }
        input_sha256 = canonical_json_sha256(input_document)
        report_id = stable_ulid(
            "relationship-observation",
            person_id,
            window_days,
            day.isoformat(),
            timezone_name,
        )
        now = self._now()
        provenance = _provenance(generator, result, "relationship_observation")
        with self._uow_factory() as uow:
            generation = _add_generation(
                uow,
                generator,
                input_sha256,
                {
                    "kind": "relationship_observation",
                    "person_id": person_id,
                    "window_days": window_days,
                    "end_date": day.isoformat(),
                    "timezone": timezone_name,
                },
                now,
            )
            revision = uow.insights.next_relationship_revision(report_id)
            _assert_evidence_current(uow, allowed_events, allowed_utterances)
            uow.insights.add_relationship(
                report_id=report_id,
                revision=revision,
                person_id=person_id,
                window_days=window_days,
                end_date=day.isoformat(),
                timezone=timezone_name,
                period_start=_datetime(period_start),
                period_end=_datetime(period_end),
                verified_facts=facts,
                observations=observations,
                input_sha256=input_sha256,
                generation_id=generation.generation_id,
                provenance=provenance,
                status="active",
                created_by="model:codex",
                created_at=_datetime(now),
            )
            _link_evidence(
                uow,
                "relationship_observation",
                report_id,
                revision,
                allowed_events,
                allowed_utterances,
                now,
            )
            uow.insights.add_operation(
                new_ulid(),
                "relationship_observation",
                report_id,
                revision,
                "generate",
                "model:codex",
                {"input_sha256": input_sha256},
                _datetime(now),
            )
            uow.audit.append(
                "insight.relationship.generated",
                "model:codex",
                "relationship_observation",
                report_id,
                {"revision": revision, "input_sha256": input_sha256},
            )
            return uow.insights.relationship(report_id)

    def relationships(
        self, person_id: str | None = None, limit: int = 100
    ) -> tuple[dict[str, Any], ...]:
        if not 1 <= limit <= 500:
            raise ValueError("relationship observation limit must be between 1 and 500")
        with self._uow_factory() as uow:
            return uow.insights.list_relationships(person_id, limit)

    def relationship(self, report_id: str) -> dict[str, Any]:
        with self._uow_factory() as uow:
            return uow.insights.relationship(report_id)

    def revise_relationship(
        self, report_id: str, raw_observations: Iterable[dict[str, Any]]
    ) -> dict[str, Any]:
        now = self._now()
        with self._uow_factory() as uow:
            current = uow.insights.relationship(report_id)
            event_ids, utterance_ids = _report_evidence_ids(current)
            try:
                observations = _validated_observations(
                    tuple(raw_observations), event_ids, utterance_ids
                )
            except InsightGenerationFailed as exc:
                raise ValueError(str(exc)) from exc
            revision = uow.insights.next_relationship_revision(report_id)
            uow.insights.add_relationship(
                **_relationship_copy_values(
                    current,
                    revision,
                    observations=observations,
                    status="active",
                    created_by="desktop-user",
                    created_at=_datetime(now),
                )
            )
            _copy_report_evidence(uow, current, revision, now)
            uow.insights.add_operation(
                new_ulid(),
                "relationship_observation",
                report_id,
                revision,
                "revise",
                "desktop-user",
                {"before_revision": current["revision"]},
                _datetime(now),
            )
            uow.audit.append(
                "insight.relationship.revised",
                "desktop-user",
                "relationship_observation",
                report_id,
                {"revision": revision, "before_revision": current["revision"]},
            )
            return uow.insights.relationship(report_id)

    def retract_relationship(self, report_id: str) -> dict[str, Any]:
        return self._change_relationship_status(report_id, "retracted", "retract")

    def undo_relationship(self, report_id: str) -> dict[str, Any]:
        now = self._now()
        with self._uow_factory() as uow:
            current = uow.insights.relationship(report_id)
            operation = uow.insights.latest_relationship_operation(report_id)
            if operation is None or operation["kind"] != "retract":
                raise ValueError("latest relationship operation cannot be undone")
            revision = uow.insights.next_relationship_revision(report_id)
            uow.insights.add_relationship(
                **_relationship_copy_values(
                    current,
                    revision,
                    observations=current["observations"],
                    status="active",
                    created_by="desktop-user",
                    created_at=_datetime(now),
                )
            )
            _copy_report_evidence(uow, current, revision, now)
            uow.insights.add_operation(
                new_ulid(),
                "relationship_observation",
                report_id,
                revision,
                "restore",
                "desktop-user",
                {"before_revision": current["revision"]},
                _datetime(now),
                reverts_operation_id=operation["operation_id"],
            )
            return uow.insights.relationship(report_id)

    def close(self) -> None:
        if self._generator is not None:
            self._generator.close()

    def _change_relationship_status(
        self, report_id: str, status: str, operation_kind: str
    ) -> dict[str, Any]:
        now = self._now()
        with self._uow_factory() as uow:
            current = uow.insights.relationship(report_id)
            if current["status"] == status:
                raise ValueError(f"relationship observation is already {status}")
            revision = uow.insights.next_relationship_revision(report_id)
            uow.insights.add_relationship(
                **_relationship_copy_values(
                    current,
                    revision,
                    observations=current["observations"],
                    status=status,
                    created_by="desktop-user",
                    created_at=_datetime(now),
                )
            )
            _copy_report_evidence(uow, current, revision, now)
            uow.insights.add_operation(
                new_ulid(),
                "relationship_observation",
                report_id,
                revision,
                operation_kind,
                "desktop-user",
                {"before_revision": current["revision"]},
                _datetime(now),
            )
            return uow.insights.relationship(report_id)

    def _require_generator(self) -> InsightModelGenerator:
        if self._generator is None:
            raise InsightGenerationUnavailable("Codex insight generation is disabled")
        return self._generator


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


def _validated_narrative(
    raw: dict[str, tuple[dict[str, Any], ...]],
    allowed_events: set[str],
    allowed_utterances: set[str],
) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(raw, dict) or set(raw) != set(DAILY_NARRATIVE_SECTIONS):
        raise InsightGenerationFailed("Codex daily narrative shape is invalid")
    result: dict[str, list[dict[str, Any]]] = {}
    for section in DAILY_NARRATIVE_SECTIONS:
        values = raw[section]
        if not isinstance(values, (list, tuple)) or len(values) > 30:
            raise InsightGenerationFailed("Codex daily narrative section is invalid")
        result[section] = [
            _narrative_item(value, allowed_events, allowed_utterances).as_dict()
            for value in values
        ]
    return result


def _validated_observations(
    raw: Iterable[dict[str, Any]],
    allowed_events: set[str],
    allowed_utterances: set[str],
) -> list[dict[str, Any]]:
    values = tuple(raw)
    if len(values) > 30 or any(not isinstance(value, dict) for value in values):
        raise InsightGenerationFailed("relationship observations are invalid")
    return [
        _observation(value, allowed_events, allowed_utterances).as_dict()
        for value in values
    ]


def _narrative_item(
    value: dict[str, Any], allowed_events: set[str], allowed_utterances: set[str]
) -> NarrativeItem:
    required = {"text", "evidence_event_ids", "evidence_utterance_ids"}
    if set(value) != required or not isinstance(value["text"], str):
        raise InsightGenerationFailed("Codex narrative item fields are invalid")
    event_ids = _string_ids(value["evidence_event_ids"], "event")
    utterance_ids = _string_ids(value["evidence_utterance_ids"], "utterance")
    _ensure_subset(event_ids, allowed_events, "event")
    _ensure_subset(utterance_ids, allowed_utterances, "utterance")
    try:
        return NarrativeItem(value["text"], event_ids, utterance_ids)
    except ValueError as exc:
        raise InsightGenerationFailed(str(exc)) from exc


def _observation(
    value: dict[str, Any], allowed_events: set[str], allowed_utterances: set[str]
) -> ModelObservation:
    required = {
        "text",
        "confidence",
        "rationale",
        "evidence_event_ids",
        "evidence_utterance_ids",
    }
    confidence = value.get("confidence")
    if (
        set(value) != required
        or not isinstance(value.get("text"), str)
        or not isinstance(value.get("rationale"), str)
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
    ):
        raise InsightGenerationFailed("relationship observation fields are invalid")
    event_ids = _string_ids(value["evidence_event_ids"], "event")
    utterance_ids = _string_ids(value["evidence_utterance_ids"], "utterance")
    _ensure_subset(event_ids, allowed_events, "event")
    _ensure_subset(utterance_ids, allowed_utterances, "utterance")
    try:
        return ModelObservation(
            text=value["text"],
            evidence_event_ids=event_ids,
            evidence_utterance_ids=utterance_ids,
            confidence=float(confidence),
            rationale=value["rationale"],
        )
    except ValueError as exc:
        raise InsightGenerationFailed(str(exc)) from exc


def _add_generation(
    uow: UnitOfWork,
    generator: InsightModelGenerator,
    input_sha256: str,
    input_scope: dict[str, Any],
    now: datetime,
) -> GenerationRecord:
    number = uow.knowledge.next_generation_number(
        KnowledgeLayer.MEMORY.value,
        "codex-insights",
        generator.producer_version,
        generator.model_label,
        generator.prompt_version,
        generator.extractor_version,
        input_sha256,
    )
    generation = GenerationRecord(
        generation_id=new_ulid(),
        layer=KnowledgeLayer.MEMORY,
        producer="codex-insights",
        producer_version=generator.producer_version,
        model=generator.model_label,
        prompt_version=generator.prompt_version,
        extractor_version=generator.extractor_version,
        input_scope=input_scope,
        input_sha256=input_sha256,
        generation_number=number,
        status=GenerationStatus.SUCCEEDED,
        created_at=now,
        completed_at=now,
    )
    if not uow.knowledge.add_generation(generation):
        raise RuntimeError("insight generation identity collision")
    return generation


def _link_evidence(
    uow: UnitOfWork,
    insight_type: str,
    insight_id: str,
    revision: int,
    event_ids: set[str],
    utterance_ids: set[str],
    now: datetime,
) -> None:
    event_revisions, utterance_revisions = uow.insights.evidence_revisions(
        tuple(sorted(event_ids)), tuple(sorted(utterance_ids))
    )
    for event_id, input_revision in sorted(event_revisions.items()):
        uow.insights.add_evidence(
            new_ulid(),
            insight_type,
            insight_id,
            revision,
            event_id,
            input_revision,
            None,
            None,
            _datetime(now),
        )
        uow.derivations.add_dependency(
            DerivationDependency(
                insight_type,
                insight_id,
                revision,
                "event",
                event_id,
                input_revision,
                now,
            )
        )
    for utterance_id, input_revision in sorted(utterance_revisions.items()):
        uow.insights.add_evidence(
            new_ulid(),
            insight_type,
            insight_id,
            revision,
            None,
            None,
            utterance_id,
            input_revision,
            _datetime(now),
        )
        uow.derivations.add_dependency(
            DerivationDependency(
                insight_type,
                insight_id,
                revision,
                "utterance",
                utterance_id,
                input_revision,
                now,
            )
        )


def _assert_evidence_current(
    uow: UnitOfWork, event_ids: set[str], utterance_ids: set[str]
) -> None:
    events, utterances = uow.insights.evidence_revisions(
        tuple(sorted(event_ids)), tuple(sorted(utterance_ids))
    )
    if set(events) != event_ids or set(utterances) != utterance_ids:
        raise InsightGenerationFailed("insight evidence changed before persistence")


def _copy_report_evidence(
    uow: UnitOfWork, current: dict[str, Any], revision: int, now: datetime
) -> None:
    event_ids, utterance_ids = _report_evidence_ids(current)
    _link_evidence(
        uow,
        "relationship_observation",
        current["report_id"],
        revision,
        event_ids,
        utterance_ids,
        now,
    )


def _relationship_copy_values(
    current: dict[str, Any],
    revision: int,
    *,
    observations: list[dict[str, Any]],
    status: str,
    created_by: str,
    created_at: str,
) -> dict[str, Any]:
    return {
        "report_id": current["report_id"],
        "revision": revision,
        "person_id": current["person_id"],
        "window_days": current["window_days"],
        "end_date": current["end_date"],
        "timezone": current["timezone"],
        "period_start": current["period_start"],
        "period_end": current["period_end"],
        "verified_facts": current["verified_facts"],
        "observations": observations,
        "input_sha256": current["input_sha256"],
        "generation_id": current["generation_id"],
        "provenance": current["provenance"],
        "status": status,
        "created_by": created_by,
        "created_at": created_at,
    }


def _report_evidence_ids(current: dict[str, Any]) -> tuple[set[str], set[str]]:
    return (
        {
            str(value["event_id"])
            for value in current["evidence"]
            if value.get("event_id")
        },
        {
            str(value["utterance_id"])
            for value in current["evidence"]
            if value.get("utterance_id")
        },
    )


def _allowed_evidence(
    events: Iterable[dict[str, Any]], quotes: Iterable[dict[str, Any]]
) -> tuple[set[str], set[str]]:
    event_ids: set[str] = set()
    utterance_ids: set[str] = set()
    for value in events:
        event_id = value.get("event_id")
        if isinstance(event_id, str):
            event_ids.add(event_id)
        evidence_ids = value.get("evidence_utterance_ids", ())
        if isinstance(evidence_ids, (list, tuple)):
            utterance_ids.update(item for item in evidence_ids if isinstance(item, str))
        utterance_id = value.get("utterance_id")
        if isinstance(utterance_id, str):
            utterance_ids.add(utterance_id)
    for value in quotes:
        utterance_id = value.get("utterance_id")
        if isinstance(utterance_id, str):
            utterance_ids.add(utterance_id)
    return event_ids, utterance_ids


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


__all__ = [
    "DailyInsightService",
    "InsightGenerationFailed",
    "InsightGenerationUnavailable",
    "observations_from_dict",
]
