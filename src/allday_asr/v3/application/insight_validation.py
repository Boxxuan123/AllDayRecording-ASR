from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from allday_asr.v3.domain.ids import new_ulid
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
    InsightModelGenerator,
)
from allday_asr.v3.ports.repositories import UnitOfWork

from .insight_errors import InsightGenerationFailed
from .insight_projections import (
    _datetime,
    _ensure_subset,
    _string_ids,
)


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
