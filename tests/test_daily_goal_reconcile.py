"""Anonymous goal facets and bounded second-pass provenance, not review lookup."""

import copy
import json
import pytest
from allday_asr.v3.domain.daily_goal_reconcile import (
    candidate_description,
    validate_groups,
    join_group,
)
from allday_asr.v3.domain.daily_semantics import reconcile_segments, semantic_payload
from allday_asr.v3.ports.daily_semantics import DailyReconciliationResult
from tests import test_daily_semantics as patterns

source = patterns.source


def candidates():
    data = patterns.rows(count=3, step=30)
    items = patterns.pieces(
        data, ["testing-facet", "routine-chatter", "interface-facet"]
    )
    items[1]["importance"] = "LOW"
    # These are substantial independent source groups, not a one-row A-B-A heuristic.
    return reconcile_segments(items)


def group(indices, refs, key="new:0", reason="same_overarching_goal"):
    return {
        "candidate_indices": indices,
        "event_key": key,
        "goal": "anonymous shared goal",
        "title": "评审匿名设备方案",
        "title_evidence_utterance_ids": refs,
        "importance": "MEDIUM",
        "importance_reason": "evidenced meaningful goal",
        "importance_evidence_utterance_ids": refs,
        "event_type": "conversation",
        "reason": reason,
        "confidence": 0.8,
    }


def test_different_facet_keys_merge_around_retained_secondary_group():
    values = candidates()
    descriptions = [candidate_description(e, i) for i, e in enumerate(values)]
    groups = [
        group([0, 2], [descriptions[0]["evidence"][0]["utterance_id"]]),
        group(
            [1],
            [descriptions[1]["evidence"][0]["utterance_id"]],
            key="new:1",
            reason="distinct_goals",
        ),
    ]
    validate_groups(groups, values, {}, descriptions)
    merged = join_group(
        groups[0], values, None, "overall-goal", {"model": "anonymous-double"}
    )
    payload = semantic_payload(merged, "anonymous-day", "UTC")
    assert len(payload["evidence_snapshots"]) == 2
    assert payload["title"] == groups[0]["title"]
    assert payload["reconciliation"]["goal_merge_reason"] == "same_overarching_goal"
    assert payload["semantic_reconciliation_provenance"]
    assert payload["summary_claims"] == [
        values[0]["segments"][0]["claims"][0],
        values[2]["segments"][0]["claims"][0],
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        "omit",
        "duplicate",
        "unknown_key",
        "invented_ref",
        "importance_ref",
        "activity",
        "raw_title",
    ],
)
def test_goal_group_validation_rejects_untrusted_assignments(mutation):
    values = candidates()
    descriptions = [candidate_description(e, i) for i, e in enumerate(values)]
    groups = [group([0, 1, 2], [descriptions[0]["evidence"][0]["utterance_id"]])]
    if mutation == "omit":
        groups[0]["candidate_indices"] = [0, 1]
    elif mutation == "duplicate":
        groups[0]["candidate_indices"] = [0, 1, 1, 2]
    elif mutation == "unknown_key":
        groups[0]["event_key"] = "unsupported-old-goal"
    elif mutation == "invented_ref":
        groups[0]["title_evidence_utterance_ids"] = ["invented"]
    elif mutation == "activity":
        groups[0]["event_type"] = "activity"
        groups[0]["reason"] = "same_real_world_activity"
    elif mutation == "importance_ref":
        groups[0]["importance_evidence_utterance_ids"] = ["invented"]
    else:
        groups[0]["title"] = "讨论片段：原文"
    with pytest.raises(ValueError):
        validate_groups(groups, values, {}, descriptions)


def test_second_pass_can_refuse_a_tentative_first_pass_key_join():
    data = patterns.rows(count=2)
    values = reconcile_segments(
        patterns.pieces(data, ["tentative", "tentative"]), merge_keys=False
    )
    assert len(values) == 2
    descriptions = [candidate_description(e, i) for i, e in enumerate(values)]
    assert all("event_key" not in d and "micro_key" in d for d in descriptions)
    assert "event_key" in candidate_description(values[0], -1)
    groups = [
        group(
            [i],
            [d["evidence"][0]["utterance_id"]],
            key=f"new:{i}",
            reason="distinct_goals",
        )
        for i, d in enumerate(descriptions)
    ]
    validate_groups(groups, values, {}, descriptions)
    assert len([join_group(g, values, None, g["event_key"], {}) for g in groups]) == 2


def test_grouping_cannot_bridge_hours_even_with_same_goal():
    values = candidates()
    late = copy.deepcopy(values[-1])
    late["evidence"][0]["start_at"] = "2026-08-01T16:00:00+00:00"
    late["evidence"][0]["end_at"] = "2026-08-01T16:00:10+00:00"
    descriptions = [candidate_description(late, 0)]
    g = group(
        [0], [descriptions[0]["evidence"][0]["utterance_id"]], key=values[0]["key"]
    )
    with pytest.raises(ValueError, match="long source gap"):
        validate_groups([g], [late], {values[0]["key"]: values[0]}, descriptions)


def test_final_goal_importance_uses_grounded_whole_activity_not_fragment_peak():
    values = candidates()
    for event in values:
        for piece in event["segments"]:
            piece["importance"] = "LOW"
            piece["event_type"] = "activity"
    descriptions = [candidate_description(e, i) for i, e in enumerate(values)]
    g = group([0, 1, 2], [descriptions[0]["evidence"][0]["utterance_id"]])
    assert descriptions[0]["activity_context"]
    assert set(descriptions[0]["activity_context"][0]["evidence_utterance_ids"]) <= {
        r["utterance_id"] for r in descriptions[0]["evidence"]
    }
    g["event_type"] = "activity"
    validate_groups([g], values, {}, descriptions)
    event = join_group(g, values, None, "activity", {})
    payload = semantic_payload(event, "anonymous-day", "UTC")
    assert payload["importance"] == "MEDIUM"
    assert payload["category"] == "activity"
    assert (
        payload["importance_evidence_utterance_ids"]
        == g["importance_evidence_utterance_ids"]
    )
    g["importance"] = "LOW"
    low = semantic_payload(
        join_group(g, values, None, "routine", {}), "anonymous-day", "UTC"
    )
    assert low["summary_visibility"] == "secondary"


class GoalAnalyzer(patterns.AnonymousAnalyzer):
    reconcile_prompt_version = "anonymous-goal.1"
    reconcile_calls = 0

    def reconcile(self, request):
        self.reconcile_calls += 1
        descriptions = request["candidates"]
        groups = [
            group(
                list(range(len(descriptions))),
                [descriptions[0]["evidence"][0]["utterance_id"]],
            )
        ]
        return DailyReconciliationResult(
            tuple(groups),
            {"provider": "synthetic", "model": self.model_label, "remote": False},
        )


def test_goal_pass_cache_idempotency_and_projection_provenance(source):
    analyzer = GoalAnalyzer()
    source[1]._daily_analyzer = analyzer
    first = patterns.refresh(source)
    second = patterns.refresh(source)
    assert analyzer.calls == analyzer.reconcile_calls == 1
    assert first["events"][0]["event_id"] == second["events"][0]["event_id"]
    assert (
        first["events"][0]["semantic_reconciliation_provenance"][0]["candidate_count"]
        == 1
    )


def test_sync_requires_completed_goal_cache_and_never_calls_model(source):
    analyzer = GoalAnalyzer()
    source[1]._daily_analyzer = analyzer
    # The first pass is cached using a double without the goal method.
    first_pass = patterns.AnonymousAnalyzer()
    source[1]._daily_analyzer = first_pass
    patterns.refresh(source)
    source[1]._daily_analyzer = analyzer
    result = source[1].refresh_daily("2026-09-01", allow_model=False)
    assert result["semantic_status"] == "pending"
    assert analyzer.calls == analyzer.reconcile_calls == 0


def test_final_normalization_merges_facets_retains_both_stage_provenance_and_cache(
    source,
):
    patterns.add(source, text="匿名设备还有考核与后续安排")

    class Normalizer(GoalAnalyzer):
        normalization_prompt_version = "anonymous-normalization.1"
        normalization_calls = 0

        def analyze(self, request):
            self.calls += 1
            return patterns.DailySemanticResult(
                tuple(
                    patterns.segment(i, i, f"new:{i}")
                    for i in range(len(request["utterances"]))
                ),
                {},
            )

        def reconcile(self, request):
            self.reconcile_calls += 1
            return DailyReconciliationResult(
                tuple(
                    group([i], [d["evidence"][0]["utterance_id"]], key=f"new:{i}")
                    for i, d in enumerate(request["candidates"])
                ),
                {},
            )

        def normalize(self, request):
            self.normalization_calls += 1
            assert all(
                "candidate_key" in d
                and "micro_key" not in d
                and "explicit_outcome" in d
                for d in request["candidates"]
            )
            result = DailyReconciliationResult(
                (
                    group(
                        list(range(len(request["candidates"]))),
                        [request["candidates"][0]["evidence"][0]["utterance_id"]],
                    ),
                ),
                {},
            )
            result.groups[0]["title"] = "已经到达未被来源支持的新地点"
            return result

    analyzer = Normalizer()
    source[1]._daily_analyzer = analyzer
    first = source[1].refresh_daily("2026-09-01")
    second = source[1].refresh_daily("2026-09-01", allow_model=False)
    assert (
        analyzer.calls == analyzer.reconcile_calls == analyzer.normalization_calls == 1
    )
    assert first["revision"] == second["revision"]
    assert len(first["objective"]["events"]) == 1
    event = first["objective"]["events"][0]
    assert event["title"] == "评审匿名设备方案"
    assert (
        event["reconciliation"]["title_selection_policy"]
        == "dominant_supported_goal_title"
    )
    assert (
        event["reconciliation"]["normalization_proposed_title"]
        == "已经到达未被来源支持的新地点"
    )
    history = first["objective"]["events"][0]["semantic_reconciliation_provenance"]
    assert len({p["input_sha256"] for p in history}) == 2
    assert len(first["objective"]["events"][0]["evidence_snapshots"]) == 2


def test_invalid_goal_cache_is_retried_only_on_explicit_generation(source):
    analyzer = GoalAnalyzer()
    source[1]._daily_analyzer = analyzer
    patterns.refresh(source)
    with source[0].database.transaction() as c:
        row = c.execute(
            "SELECT generation_id,input_scope_json FROM generation_records WHERE producer='daily-semantic-reconcile'"
        ).fetchone()
        scope = json.loads(row["input_scope_json"])
        scope["groups"][0]["importance_evidence_utterance_ids"] = ["invented"]
        c.execute(
            "UPDATE generation_records SET input_scope_json=? WHERE generation_id=?",
            (json.dumps(scope), row["generation_id"]),
        )
    assert (
        source[1].refresh_daily("2026-09-01", allow_model=False)["semantic_status"]
        == "pending"
    )
    assert analyzer.calls == analyzer.reconcile_calls == 1
    patterns.refresh(source)
    patterns.refresh(source)
    assert analyzer.calls == 1 and analyzer.reconcile_calls == 2
