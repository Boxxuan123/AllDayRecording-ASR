"""Bounded model calls outside writer transactions; reuse generation records."""

import copy
import json
from allday_asr.v3.domain.daily_semantics import (
    micro_batches,
    validate_segments,
    reconcile_segments,
)
from allday_asr.v3.domain.daily_events import seconds
from allday_asr.v3.domain.hashing import canonical_json_sha256
from allday_asr.v3.domain.ids import new_ulid, stable_ulid
from allday_asr.v3.domain.knowledge import (
    GenerationRecord,
    GenerationStatus,
    KnowledgeLayer,
)
from .daily_goal_reconcile import reconcile_goals


def analyze_daily(service, rows, *, allow_model=True):
    analyzer = service._daily_analyzer
    if analyzer is None:
        return None, "semantic_model_unavailable"
    segments, context, previous = [], {}, []
    batches = micro_batches(rows)
    for number, batch in enumerate(batches):
        if not any(
            len(r["text"].strip("嗯啊哦噢呃唔哼哈，。！？ ")) >= 4 or r.get("task_ids")
            for r in batch
        ):
            continue
        active = {
            key: value
            for key, value in context.items()
            if seconds(batch[0]["start_at"]) - seconds(value["end_at"]) <= 300
        }
        payload = {
            "version": analyzer.prompt_version,
            "utterances": [
                {
                    "index": i,
                    "text": r["text"],
                    "start_at": r["start_at"],
                    "end_at": r["end_at"],
                    "speaker_ref": r["participant"]["key"],
                    "speaker_kind": r["participant"]["kind"],
                    "task_ids": r.get("task_ids", []),
                }
                for i, r in enumerate(batch)
            ],
            "open_context": list(active.values()),
            "previous_context": previous[-5:],
            "lookahead": [
                {"text": r["text"], "speaker_ref": r["participant"]["key"]}
                for r in (batches[number + 1][:5] if number + 1 < len(batches) else [])
            ],
        }
        digest = canonical_json_sha256(
            {
                "payload": payload,
                "model": analyzer.model_label,
                "source_revisions": [(r["utterance_id"], r["revision"]) for r in batch],
            }
        )
        with service._uow_factory().reading() as uow:
            cached = uow.insights.daily_semantic_cache(digest)
        if cached:
            try:
                validate_segments(cached["result"], batch, set(active))
            except (ValueError, KeyError, TypeError):
                # A cache created by an older validator is never trusted as fact.
                # Explicit generation may retry; projection refresh stays local.
                cached = None
        if cached:
            result = cached["result"]
            provenance = cached["provenance"]
        else:
            if not allow_model:
                return None, "semantic_generation_pending"
            # Invalid indices never become facts. Retry the same bounded input;
            # keep prior published events if all attempts fail.
            request = payload
            for attempt in range(3):
                try:
                    result_object = analyzer.analyze(request)
                    result = list(result_object.segments)
                    validate_segments(result, batch, set(active))
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    request = {
                        **payload,
                        "validation_feedback": {
                            "status": "previous_output_rejected",
                            "source_index_range": [0, len(batch) - 1],
                            "requirements": (
                                "Return the complete strict schema. Inclusive ranges partition "
                                "every current index exactly once. Every title/claim/outcome "
                                "index belongs to its OWN segment range. Continuation keys "
                                "must be supplied open_context keys; new keys use new:N. "
                                "Do not copy indices from prior context or future lookahead."
                            ),
                        },
                    }
            provenance = {
                **result_object.provenance,
                "attempts": attempt + 1,
                "input_characters": len(json.dumps(request, ensure_ascii=False)),
                "source_characters": sum(len(r["text"]) for r in batch),
                "source_count": len(batch),
                "input_sha256": digest,
                "analyzed_input_sha256": canonical_json_sha256(request),
            }
            now = service._now()
            with service._uow_factory() as uow:
                generation = GenerationRecord(
                    new_ulid(),
                    KnowledgeLayer.EVENT,
                    "daily-semantic",
                    analyzer.producer_version,
                    analyzer.model_label,
                    analyzer.prompt_version,
                    "daily-semantic-v1.1",
                    {"result": result, "provenance": provenance},
                    digest,
                    uow.knowledge.next_generation_number(
                        "event",
                        "daily-semantic",
                        analyzer.producer_version,
                        analyzer.model_label,
                        analyzer.prompt_version,
                        "daily-semantic-v1.1",
                        digest,
                    ),
                    GenerationStatus.SUCCEEDED,
                    now,
                    now,
                )
                uow.knowledge.add_generation(generation)
        validate_segments(result, batch, set(active))
        new_keys = {}
        for output in result:
            output = copy.deepcopy(output)
            selected = batch[output["start_index"] : output["end_index"] + 1]
            key = output["event_key"]
            if key.startswith("new:"):
                key = new_keys.setdefault(
                    key,
                    stable_ulid("daily-semantic-context", selected[0]["utterance_id"]),
                )
            output["key"], output["evidence"], output["provenance"] = (
                key,
                selected,
                provenance,
            )
            output["title_evidence_utterance_ids"] = [
                batch[i]["utterance_id"] for i in output["title_evidence_indices"]
            ]
            output["claims"] = [
                {
                    "text": c["text"],
                    "evidence_utterance_ids": [
                        batch[i]["utterance_id"] for i in c["evidence_indices"]
                    ],
                }
                for c in output["claims"]
            ]
            if output["explicit_outcome"]:
                outcome = output["explicit_outcome"]
                output["explicit_outcome"] = {
                    "kind": outcome["kind"],
                    "quote": outcome["quote"],
                    "evidence_utterance_ids": [
                        batch[i]["utterance_id"] for i in outcome["evidence_indices"]
                    ],
                }
            segments.append(output)
            if output["classification"] == "CORE":
                context[key] = {
                    "event_key": key,
                    "core_topic": output["core_topic"],
                    "title": output["title"],
                    "summary": [c["text"] for c in output["claims"]],
                    "end_at": selected[-1]["end_at"],
                    "importance": output["importance"],
                }
        previous = [
            {"text": r["text"], "speaker_ref": r["participant"]["key"]}
            for r in batch[-5:]
        ]
    # The independent goal pass must be able to refuse first-pass key joins.
    # Never collapse chunks into an indivisible candidate before that decision.
    candidates = reconcile_segments(
        segments, merge_keys=not callable(getattr(analyzer, "reconcile", None)),
        preliminary_gate=not getattr(analyzer, "reconcile_schema_version", "").startswith("daily-semantic-v1.2"),
    )
    # Preserve the semantic partition locally, including suppressed micro/filler.
    # This audit uses existing generation provenance; it is not a new event layer.
    audit = [{"key": s["key"], "classification": s["classification"], "title": s["title"],
              "claims": s["claims"], "explicit_outcome": s["explicit_outcome"],
              "source_revisions": {r["utterance_id"]: r["revision"] for r in s["evidence"]},
              "provenance": s["provenance"]} for s in segments]
    if audit:
        digest = canonical_json_sha256(audit)
        with service._uow_factory().reading() as uow:
            existing = uow.insights.daily_semantic_cache(digest, "daily-semantic-audit")
        if not existing:
            now = service._now()
            with service._uow_factory() as uow:
                uow.knowledge.add_generation(GenerationRecord(
                    new_ulid(), KnowledgeLayer.EVENT, "daily-semantic-audit", "1", "local", "1", "1",
                    {"result": audit}, digest,
                    uow.knowledge.next_generation_number("event", "daily-semantic-audit", "1", "local", "1", "1", digest),
                    GenerationStatus.SUCCEEDED, now, now))
    return reconcile_goals(service, candidates, allow_model=allow_model)
