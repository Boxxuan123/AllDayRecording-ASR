from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from allday_asr.v3.domain.hashing import canonical_json_sha256
from allday_asr.v3.domain.ids import new_ulid, stable_ulid
from allday_asr.v3.ports.insight_generation import (
    DailyInsightModelRequest,
    RelationshipInsightModelRequest,
)

from .insight_errors import InsightGenerationFailed
from .insight_inputs import _daily_inputs, _relationship_inputs
from .insight_projections import _date, _datetime, _effort, _period, _provenance
from .insight_validation import (
    _add_generation,
    _allowed_evidence,
    _assert_evidence_current,
    _link_evidence,
    _validated_narrative,
    _validated_observations,
)


class InsightGenerationMixin:
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
