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
    trace = []
    goals, error = _reconcile_round(
        service, candidates, allow_model=allow_model, trace=trace
    )
    decisions = [
        g
        for g in goals or []
        if not g.get("materialization", {}).get("materialize", True)
    ]
    reviewable = [
        g
        for g in decisions
        if callable(getattr(analyzer, "normalize", None))
        and getattr(analyzer, "normalization_review_prompt_version", None)
        and not g.get("routine_logistics", False)
    ]
    review_keys = {g["key"] for g in reviewable}
    decisions = [g for g in decisions if g["key"] not in review_keys]
    if goals is not None:
        goals = [
            (
                {**g, "reconsider_materialization": True}
                if g["key"] in review_keys
                else g
            )
            for g in goals
            if g.get("materialization", {}).get("materialize", True)
            or g["key"] in review_keys
        ]
    if (
        error
        or goals is None
        or (len(goals) < 2 and not reviewable)
        or not callable(getattr(analyzer, "normalize", None))
    ):
        return _finish(service, goals, error, trace, decisions + (goals or []))
    final, error = _reconcile_round(
        service, goals, allow_model=allow_model, normalizing=True, trace=trace
    )
    result = (
        (
            constrain_normalized_titles(
                [
                    g
                    for g in final
                    if g.get("materialization", {}).get("materialize", True)
                ],
                goals,
            ),
            None,
        )
        if final is not None
        else (None, error)
    )
    return _finish(service, *result, trace, decisions + (final or []))


def _finish(service, goals, error, trace, decisions):
    if goals is None or error or not trace:
        return goals, error
    scope = {
        "result": {
            "used_reconcile_inputs": trace,
            "final_semantic_keys": [g["key"] for g in goals],
            "materialization_decisions": [
                {
                    "semantic_key": g["key"],
                    "title": g.get("goal_title"),
                    "topic_purity": g.get("topic_purity"),
                    "materialization": g.get("materialization"),
                    "source_roles": g.get("source_roles", []),
                    "source_revisions": {
                        r["utterance_id"]: r["revision"] for r in g["evidence"]
                    },
                }
                for g in decisions
            ],
        }
    }
    digest = canonical_json_sha256(scope)
    producer = "daily-semantic-manifest"
    with service._uow_factory().reading() as uow:
        cached = uow.insights.daily_semantic_cache(digest, producer)
    if not cached:
        now = service._now()
        with service._uow_factory() as uow:
            uow.knowledge.add_generation(
                GenerationRecord(
                    new_ulid(),
                    KnowledgeLayer.EVENT,
                    producer,
                    "1",
                    "local",
                    "1",
                    "1",
                    scope,
                    digest,
                    uow.knowledge.next_generation_number(
                        "event", producer, "1", "local", "1", "1", digest
                    ),
                    GenerationStatus.SUCCEEDED,
                    now,
                    now,
                )
            )
    return goals, error


def _reconcile_round(
    service, candidates, *, allow_model, normalizing=False, trace=None
):
    analyzer = service._daily_analyzer
    prompt_version = (
        analyzer.normalization_prompt_version
        if normalizing
        else analyzer.reconcile_prompt_version
    )
    infer = analyzer.normalize if normalizing else analyzer.reconcile
    schema_version = getattr(
        analyzer, "reconcile_schema_version", "daily-semantic-v1.1-reconcile-schema.2"
    )
    require_purity = schema_version.startswith("daily-semantic-v1.2")

    def describe(event, index):
        value = candidate_description(
            event,
            index,
            include_outcome=normalizing or require_purity,
            include_scope=normalizing
            and bool(getattr(analyzer, "normalization_scope_prompt_version", None)),
        )
        if normalizing and index >= 0:
            value["candidate_key"] = value.pop("micro_key")
            if event.get("reconsider_materialization"):
                value["reconsider_materialization"] = True
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
            "version": (
                analyzer.normalization_review_prompt_version
                if normalizing
                and any(d.get("reconsider_materialization") for d in descriptions)
                else getattr(
                    analyzer, "normalization_scope_prompt_version", prompt_version
                )
                if normalizing
                else prompt_version
            ),
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
                    require_purity=require_purity,
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
                        require_purity=require_purity,
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
                    request["version"],
                    schema_version,
                    {
                        "groups": groups,
                        "provenance": provenance,
                        "request": request,
                        "source_candidates": [
                            {
                                "key": e["key"],
                                "source_revisions": {
                                    r["utterance_id"]: r["revision"]
                                    for r in e["evidence"]
                                },
                            }
                            for e in source
                        ],
                    },
                    digest,
                    uow.knowledge.next_generation_number(
                        "event",
                        PRODUCER,
                        analyzer.producer_version,
                        analyzer.model_label,
                        request["version"],
                        schema_version,
                        digest,
                    ),
                    GenerationStatus.SUCCEEDED,
                    now,
                    now,
                )
                uow.knowledge.add_generation(record)
        validate_groups(
            groups,
            source,
            active,
            descriptions,
            include_outcome=normalizing,
            require_purity=require_purity,
        )
        if trace is not None:
            trace.append(digest)
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
