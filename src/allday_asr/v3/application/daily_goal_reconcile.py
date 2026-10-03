"""Bounded second pass over grounded candidates, using existing generation cache."""

import json
from allday_asr.v3.domain.daily_events import seconds
from allday_asr.v3.domain.daily_goal_reconcile import (
    candidate_description,
    validate_groups,
    join_group,
    constrain_normalized_titles,
)
from allday_asr.v3.domain.hashing import canonical_json_sha256
from allday_asr.v3.domain.ids import new_ulid, stable_ulid
from allday_asr.v3.domain.knowledge import (
    GenerationRecord,
    GenerationStatus,
    KnowledgeLayer,
)

PRODUCER = "daily-semantic-reconcile"


def reconcile_goals(service, candidates, *, allow_model=True):
    analyzer = service._daily_analyzer
    if not candidates or not callable(getattr(analyzer, "reconcile", None)):
        return candidates, None
    goals, error = _reconcile_round(service, candidates, allow_model=allow_model)
    if (
        error
        or goals is None
        or len(goals) < 2
        or not callable(getattr(analyzer, "normalize", None))
    ):
        return goals, error
    final, error = _reconcile_round(
        service, goals, allow_model=allow_model, normalizing=True
    )
    return (
        (constrain_normalized_titles(final, goals), None)
        if final is not None
        else (None, error)
    )


def _reconcile_round(service, candidates, *, allow_model, normalizing=False):
    analyzer = service._daily_analyzer
    prompt_version = (
        analyzer.normalization_prompt_version
        if normalizing
        else analyzer.reconcile_prompt_version
    )
    infer = analyzer.normalize if normalizing else analyzer.reconcile

    def describe(event, index):
        value = candidate_description(event, index, include_outcome=normalizing)
        if normalizing and index >= 0:
            value["candidate_key"] = value.pop("micro_key")
        return value

    batches = []
    for candidate in candidates:
        description = describe(candidate, 0)
        size = len(json.dumps(description, ensure_ascii=False))
        if (
            not batches
            or len(batches[-1]) >= 12
            or sum(v[1] for v in batches[-1]) + size > 6000
        ):
            batches.append([])
        batches[-1].append((candidate, size))
    current = {}
    for batch in batches:
        source = [v[0] for v in batch]
        active = {
            key: e
            for key, e in current.items()
            if seconds(source[0]["evidence"][0]["start_at"])
            - seconds(e["evidence"][-1]["end_at"])
            <= 300
        }
        descriptions = [describe(e, i) for i, e in enumerate(source)]
        request = {
            "version": prompt_version,
            "candidates": descriptions,
            "open_context": [describe(e, -1) for e in active.values()],
        }
        digest = canonical_json_sha256(
            {
                "request": request,
                "model": analyzer.model_label,
                "source_revisions": [
                    (r["utterance_id"], r["revision"])
                    for e in source
                    for r in e["evidence"]
                ],
            }
        )
        with service._uow_factory().reading() as uow:
            cached = uow.insights.daily_semantic_cache(digest, PRODUCER)
        if cached:
            try:
                validate_groups(
                    cached["groups"],
                    source,
                    active,
                    descriptions,
                    include_outcome=normalizing,
                )
            except (ValueError, KeyError, TypeError):
                cached = None
        if cached:
            groups, provenance = cached["groups"], cached["provenance"]
        else:
            if not allow_model:
                return None, "semantic_reconciliation_pending"
            analyzed_request = request
            for attempt in range(3):
                try:
                    result = infer(analyzed_request)
                    groups = list(result.groups)
                    validate_groups(
                        groups,
                        source,
                        active,
                        descriptions,
                        include_outcome=normalizing,
                    )
                    break
                except Exception as exc:
                    if attempt == 2:
                        raise
                    analyzed_request = {
                        **request,
                        "validation_feedback": {
                            "status": "previous_output_rejected",
                            "reason": str(exc)
                            if isinstance(exc, ValueError)
                            else "provider_or_output_error",
                            "requirements": "Keep all candidates exactly once. Cite only supplied evidence from the SAME group. Do not bridge source gaps over 300 seconds or claim activity without an already evidenced activity candidate.",
                        },
                    }
            provenance = {
                **result.provenance,
                "attempts": attempt + 1,
                "input_sha256": digest,
                "input_characters": len(
                    json.dumps(analyzed_request, ensure_ascii=False)
                ),
                "analyzed_input_sha256": canonical_json_sha256(analyzed_request),
                "candidate_count": len(source),
                "source_count": sum(len(d["evidence"]) for d in descriptions),
                "source_characters": sum(
                    len(r["text_excerpt"]) for d in descriptions for r in d["evidence"]
                ),
            }
            now = service._now()
            with service._uow_factory() as uow:
                record = GenerationRecord(
                    new_ulid(),
                    KnowledgeLayer.EVENT,
                    PRODUCER,
                    analyzer.producer_version,
                    analyzer.model_label,
                    prompt_version,
                    "daily-semantic-v1.1-reconcile-schema.2",
                    {"groups": groups, "provenance": provenance},
                    digest,
                    uow.knowledge.next_generation_number(
                        "event",
                        PRODUCER,
                        analyzer.producer_version,
                        analyzer.model_label,
                        prompt_version,
                        "daily-semantic-v1.1-reconcile-schema.2",
                        digest,
                    ),
                    GenerationStatus.SUCCEEDED,
                    now,
                    now,
                )
                uow.knowledge.add_generation(record)
        validate_groups(
            groups, source, active, descriptions, include_outcome=normalizing
        )
        for group in groups:
            key = group["event_key"]
            if key.startswith("new:"):
                key = stable_ulid(
                    "daily-semantic-goal",
                    source[group["candidate_indices"][0]]["evidence"][0][
                        "utterance_id"
                    ],
                )
            current[key] = join_group(group, source, current.get(key), key, provenance)
    return sorted(
        current.values(), key=lambda e: seconds(e["evidence"][0]["start_at"])
    ), None
