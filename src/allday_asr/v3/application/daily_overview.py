"""Bounded, cached synthesis before the atomic daily publication transaction."""

import json
import re
from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.knowledge import (
    GenerationRecord,
    GenerationStatus,
    KnowledgeLayer,
)

from .daily_generation_reliability import request_fingerprint, canonical_result, cache_hit, retry_allowed, checkpoint_valid, seal_checkpoint

PRODUCER = "daily-semantic-overview"


def validate_overview(content, source_ids):
    if type(content) is not dict or set(content) != {
        "headline",
        "headline_source_event_ids",
        "overview_sentences",
    }:
        raise ValueError("overview schema has missing or extra fields")
    sentences = content["overview_sentences"]
    if type(sentences) is not list or type(content["headline"]) is not str:
        raise ValueError("overview schema field types are invalid")
    if any(
        type(s) is not dict
        or set(s) != {"text", "source_event_ids"}
        or type(s["text"]) is not str
        for s in sentences
    ):
        raise ValueError("overview sentence schema is invalid")
    if not 1 <= len(sentences) <= 4 or not content["headline"].strip():
        raise ValueError(
            "overview needs a headline and one to four source-linked sentences"
        )
    citations = [
        content["headline_source_event_ids"],
        *[s["source_event_ids"] for s in sentences],
    ]
    if any(
        type(refs) is not list or any(type(ref) is not str for ref in refs)
        for refs in citations
    ):
        raise ValueError("overview citations must be string arrays")
    if any(
        not refs or len(refs) != len(set(refs)) or not set(refs) <= source_ids
        for refs in citations
    ):
        raise ValueError("overview cites missing or unprovided Final Events")
    for sentence in sentences:
        text = sentence["text"].strip()
        if not text or len(re.findall(r"[。！？!?]", text)) > 1:
            raise ValueError(
                "each overview item must contain one sentence, not concatenated cards"
            )
    if len(source_ids) > 3 and len(sentences) < 2:
        raise ValueError("normal active days need two to four overview sentences")


def prepare_overview(service, major, tasks, day, *, allow_model):
    analyzer = service._daily_analyzer
    if not major or not callable(getattr(analyzer, "overview", None)):
        return None, None
    values = []
    for event in major:
        item = {
            k: event[k]
            for k in (
                "event_id",
                "title",
                "summary",
                "start_at",
                "end_at",
                "importance",
                "linked_task_ids",
                "outcome",
            )
        }
        item["known_participants"] = [
            p for p in event["participants"] if p["kind"] != "unknown"
        ]
        tentative = [*values, item]
        if (
            len(tentative) > 20
            or len(json.dumps(tentative, ensure_ascii=False)) > 10000
        ):
            break
        values = tentative
    ids = {e["event_id"] for e in values}
    linked_tasks = [
        {
            "task_id": t["event_id"],
            "title": t["payload"].get("title", "待办"),
            "status": t["status"],
            "scheduled_at": t.get("scheduled_at") or t["payload"].get("scheduled_at"),
            "source_event_ids": [
                e["event_id"] for e in values if t["event_id"] in e["linked_task_ids"]
            ],
        }
        for t in tasks
        if any(t["event_id"] in e["linked_task_ids"] for e in values)
    ]
    request = {
        "version": analyzer.overview_prompt_version,
        "date": day,
        "major_events": values,
        "authoritative_tasks": linked_tasks,
    }
    if len(json.dumps(request, ensure_ascii=False)) > 12000:
        raise ValueError("bounded day overview exceeds its high-level input budget")
    digest = request_fingerprint(analyzer, request, {"major_sources": [e for e in major if e["event_id"] in ids], "task_sources": tasks}, analyzer.overview_prompt_version, analyzer.overview_schema_version)
    with service._uow_factory().reading() as uow:
        cached = uow.insights.daily_semantic_cache(digest, PRODUCER)
    if cached and not checkpoint_valid(cached, digest, "result"):
        cached = None
    if cached:
        try:
            cached["result"] = canonical_result(analyzer, request, cached["result"])
            validate_overview(cached["result"], ids)
        except (KeyError, TypeError, ValueError):
            cached = None
    if cached:
        cache_hit(analyzer, digest)
        return cached, None
    if not allow_model:
        return None, "semantic_overview_pending"
    analyzed = request
    for attempt in range(2):
        try:
            result = analyzer.overview(analyzed)
            from allday_asr.v3.ports.daily_semantics import DailyOverviewResult
            result = DailyOverviewResult(canonical_result(analyzer, request, result.content), result.provenance)
            validate_overview(result.content, ids)
            break
        except Exception as exc:
            if attempt == 1 or not retry_allowed(exc):
                raise
            analyzed = {
                **request,
                "validation_feedback": {
                    "status": "previous_output_rejected",
                    "reason": str(exc)
                    if isinstance(exc, ValueError)
                    else "provider_or_output_error",
                },
            }
    provenance = {
        **result.provenance,
        "input_sha256": digest,
        "attempts": attempt + 1,
        "input_characters": len(json.dumps(analyzed, ensure_ascii=False)),
        "source_count": len(values),
        "source_event_ids": sorted(ids),
        "omitted_major_event_ids": [
            e["event_id"] for e in major if e["event_id"] not in ids
        ],
        "input_kind": "major_final_events_only",
    }
    provenance = seal_checkpoint(provenance, result.content)
    cached = {"result": result.content, "provenance": provenance}
    now = service._now()
    with service._uow_factory() as uow:
        uow.knowledge.add_generation(
            GenerationRecord(
                new_ulid(),
                KnowledgeLayer.MEMORY,
                PRODUCER,
                analyzer.producer_version,
                analyzer.model_label,
                analyzer.overview_prompt_version,
                analyzer.overview_schema_version,
                cached,
                digest,
                uow.knowledge.next_generation_number(
                    "memory",
                    PRODUCER,
                    analyzer.producer_version,
                    analyzer.model_label,
                    analyzer.overview_prompt_version,
                    analyzer.overview_schema_version,
                    digest,
                ),
                GenerationStatus.SUCCEEDED,
                now,
                now,
            )
        )
    return cached, None
