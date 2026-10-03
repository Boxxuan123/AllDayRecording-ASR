"""Anonymous policy, source-purity and structured-synthesis regressions."""

from types import SimpleNamespace
from datetime import datetime, timezone, timedelta
import pytest
from tests.test_daily_semantics import rows, pieces, AnonymousAnalyzer, refresh
from tests import test_daily_semantics as patterns
from tests.test_daily_goal_reconcile import group
from allday_asr.v3.domain.daily_semantics import reconcile_segments, semantic_payload
from allday_asr.v3.domain.daily_semantics import validate_segments
from tests.test_daily_semantics import segment
from allday_asr.v3.domain.daily_goal_reconcile import (
    candidate_description,
    validate_groups,
    join_group,
)
from allday_asr.v3.application.daily_structured_summary import structured_summary
from allday_asr.v3.application.daily_overview import validate_overview
from allday_asr.v3.ports.daily_semantics import DailyOverviewResult
from allday_asr.v3.ports.daily_semantics import DailyReconciliationResult
from allday_asr.v3.application.daily_goal_reconcile import reconcile_goals

source = patterns.source


def purity(
    indices, refs, *, materialize=True, reason="sustained_meaningful_topic", basis=None
):
    return {
        "domain": "anonymous-domain",
        "specific_topic": "specific device review",
        "active_goal": "evaluate the same device",
        "entities": ["device-a"],
        "decision_context": "",
        "continuity_basis": basis
        or ("single_topic" if len(indices) == 1 else "same_concrete_project"),
        "continuity_reason": "same evidenced concrete object and evaluation",
        "source_roles": [
            {
                "candidate_index": i,
                "role": "CORE",
                "reason": "supports the same specific goal",
                "evidence_utterance_ids": [refs[n]],
            }
            for n, i in enumerate(indices)
        ],
        "materialize": materialize,
        "materialization_reason": reason,
        "materialization_evidence_utterance_ids": [refs[0]],
        "information_value": 70 if materialize else 5,
        "information_value_reason": "future recall of concrete work",
        "routine_logistics": False,
    }


def grounded(
    values, *, materialize=True, reason="sustained_meaningful_topic", basis=None
):
    descriptions = [candidate_description(e, i) for i, e in enumerate(values)]
    refs = [d["evidence"][0]["utterance_id"] for d in descriptions]
    g = group(list(range(len(values))), [refs[0]])
    g["topic_purity"] = purity(
        g["candidate_indices"],
        refs,
        materialize=materialize,
        reason=reason,
        basis=basis,
    )
    return g, descriptions


def test_broad_domain_and_same_people_cannot_establish_merge():
    data = rows(count=13, step=60)
    for row in data:
        row["end_at"] = (
            datetime.fromisoformat(row["start_at"]) + timedelta(seconds=60)
        ).isoformat()
    labels = ["exam"] * 4 + ["language"] * 4 + ["overseas-cost"] * 5
    values = reconcile_segments(
        pieces(data, labels),
    )
    g, desc = grounded(values, basis="same_domain")
    with pytest.raises(ValueError, match="weak domain"):
        validate_groups([g], values, {}, desc, require_purity=True)
    groups = []
    for i, value in enumerate(values):
        single, _ = grounded([value])
        single["candidate_indices"] = [i]
        single["event_key"] = f"new:{i}"
        single["topic_purity"]["source_roles"][0]["candidate_index"] = i
        single["topic_purity"]["specific_topic"] = [
            "exam",
            "language",
            "overseas-cost",
        ][i]
        groups.append(single)
    validate_groups(groups, values, {}, desc, require_purity=True)
    assert len([join_group(g, values, None, g["event_key"], {}) for g in groups]) == 3


def test_specific_project_long_review_one_event_without_window_cap():
    values = reconcile_segments(
        pieces(rows(count=26, step=60), [f"facet-{i}" for i in range(26)]),
        merge_keys=False,
    )
    g, desc = grounded(values)
    validate_groups([g], values, {}, desc, require_purity=True)
    event = join_group(g, values, None, "concrete-project", {})
    assert len(event["evidence"]) == 26
    assert (
        semantic_payload(event, "anonymous-day", "UTC")["reconciliation"][
            "final_duration_limit"
        ]
        is None
    )


def test_goal_scope_keeps_middle_and_final_assessment_inside_a_bounded_description():
    import json
    from allday_asr.v3.domain.daily_semantics import reconcile_segments

    data = pieces(rows(count=15), ["concrete-device"] * 15)
    for i, p in enumerate(data):
        p["title"] = f"component-{i}"
        p["importance"] = "HIGH"
        p["claims"][0]["text"] = "匿名设备的实际评审子主题。" * 30
    event = reconcile_segments(data, preliminary_gate=False)[0]
    desc = candidate_description(event, 0, include_scope=True)
    topics = desc["source_topics"]
    assert len(topics) <= 5 and len(json.dumps(topics, ensure_ascii=False)) <= 2000
    assert {"component-0", "component-7", "component-14"} <= {
        p["title"] for p in topics
    }
    assert "source_topics" not in candidate_description(event, 0)
    refs = {r["utterance_id"] for r in desc["evidence"]}
    assert data[5]["evidence"][0]["utterance_id"] in refs
    assert data[10]["evidence"][0]["utterance_id"] in refs
    assert len(refs) <= 10


def test_incidental_sources_excluded_from_evidence_claims_and_outcome():
    values = reconcile_segments(
        pieces(rows(count=3, step=20), ["device", "ordering", "device"]),
        merge_keys=False,
    )
    g, desc = grounded(values)
    g["topic_purity"]["source_roles"][1]["role"] = "INCIDENTAL"
    validate_groups([g], values, {}, desc, require_purity=True)
    event = join_group(g, values, None, "device", {})
    payload = semantic_payload(event, "anonymous-day", "UTC")
    excluded = values[1]["evidence"][0]["utterance_id"]
    assert excluded not in {r["utterance_id"] for r in payload["evidence_snapshots"]}
    assert excluded not in {
        uid for c in payload["summary_claims"] for uid in c["evidence_utterance_ids"]
    }
    assert event["source_roles"][1]["source_utterance_ids"] == [excluded]
    g["title_evidence_utterance_ids"] = [excluded]
    with pytest.raises(ValueError, match="incidental"):
        validate_groups([g], values, {}, desc, require_purity=True)


@pytest.mark.parametrize(
    "seconds,reason", [(5, "schedule_change"), (30, "explicit_decision"), (2, "task")]
)
def test_short_strong_sources_survive_preliminary_duration_gate(seconds, reason):
    data = rows(count=1)
    data[0]["end_at"] = (
        datetime.fromisoformat(data[0]["start_at"]) + timedelta(seconds=seconds)
    ).isoformat()
    data[0]["text"] = "已决定明天项目评审改到下午三点。"
    if reason == "task":
        data[0]["task_ids"] = ["confirmed-task"]
    values = reconcile_segments(
        pieces(data, ["schedule"]), merge_keys=False, preliminary_gate=False
    )
    g, desc = grounded(values, reason=reason)
    validate_groups([g], values, {}, desc, require_purity=True)
    assert join_group(g, values, None, "schedule", {})["materialization"]["materialize"]


@pytest.mark.parametrize(
    "seconds,text",
    [
        (10, "普通优惠券闲聊"),
        (20, "今天普通天气闲聊"),
        (30, "普通点菜闲聊"),
        (45, "普通道路导航"),
    ],
)
def test_low_value_stays_semantic_micro_not_final(seconds, text):
    data = rows(count=1)
    data[0]["text"] = text
    data[0]["end_at"] = (
        datetime.fromisoformat(data[0]["start_at"]) + timedelta(seconds=seconds)
    ).isoformat()
    values = reconcile_segments(
        pieces(data, ["routine"]), merge_keys=False, preliminary_gate=False
    )
    g, desc = grounded(values, materialize=False, reason="low_information_fragment")
    validate_groups([g], values, {}, desc, require_purity=True)
    micro = join_group(g, values, None, "routine", {})
    assert not micro["materialization"]["materialize"]
    assert micro["evidence"] == data and micro["segments"]


def test_task_inside_chatter_cannot_be_suppressed_or_rejected():
    values = reconcile_segments(pieces(rows(count=1), ["chat"]), merge_keys=False)
    values[0]["evidence"][0]["task_ids"] = ["confirmed-task"]
    g, desc = grounded(values, materialize=False, reason="low_information_fragment")
    with pytest.raises(ValueError, match="task"):
        validate_groups([g], values, {}, desc, require_purity=True)


@pytest.mark.parametrize(
    "routine,review_keep,source_importance",
    [
        (True, False, "MEDIUM"),
        (False, True, "MEDIUM"),
        (False, False, "MEDIUM"),
        (False, False, "LOW"),
    ],
)
def test_nonroutine_negative_gate_gets_bounded_reconsideration_not_automatic_keep(
    source, routine, review_keep, source_importance
):
    class Analyzer:
        model_label = "anonymous-value-review"
        producer_version = "test"
        reconcile_prompt_version = "anonymous-goal.1"
        reconcile_schema_version = "daily-semantic-v1.2-test"
        normalization_prompt_version = "anonymous-normalize.1"
        normalization_review_prompt_version = "anonymous-value-review.1"
        review_calls = 0

        def output(self, request, keep):
            uid = request["candidates"][0]["evidence"][0]["utterance_id"]
            g = group([0], [uid])
            g["topic_purity"] = purity(
                [0],
                [uid],
                materialize=keep,
                reason="future_memory_value" if keep else "low_information_fragment",
            )
            g["topic_purity"]["routine_logistics"] = routine
            return DailyReconciliationResult((g,), {"remote": False})

        def reconcile(self, request):
            return self.output(request, False)

        def normalize(self, request):
            self.review_calls += 1
            assert request["version"] == self.normalization_review_prompt_version
            assert request["candidates"][0]["reconsider_materialization"] is True
            assert "utterances" not in request
            return self.output(request, review_keep)

    analyzer = Analyzer()
    source[1]._daily_analyzer = analyzer
    candidates = reconcile_segments(
        pieces(rows(count=1), ["uncertain-specific-topic"]), preliminary_gate=False
    )
    for s in candidates[0]["segments"]:
        s["importance"] = source_importance
    result, error = reconcile_goals(source[1], candidates)
    assert error is None and len(result) == int(review_keep)
    assert analyzer.review_calls == int(not routine)
    repeated, error = reconcile_goals(source[1], candidates, allow_model=False)
    assert error is None and repeated == result
    assert analyzer.review_calls == int(not routine)


def test_materialization_cannot_claim_task_authority_without_task_link():
    values = reconcile_segments(pieces(rows(count=1), ["discussion"]), merge_keys=False)
    g, desc = grounded(values, reason="task")
    with pytest.raises(ValueError, match="already authoritative task"):
        validate_groups([g], values, {}, desc, require_purity=True)


@pytest.mark.parametrize("classification", ["INCIDENTAL", "FILLER"])
def test_extractor_cannot_hide_existing_task_as_noncore(classification):
    data = rows(count=1)
    data[0]["task_ids"] = ["authoritative-task"]
    with pytest.raises(ValueError, match="authoritative task source"):
        validate_segments([segment(0, 0, classification=classification)], data, set())


def test_return_to_a_does_not_make_persistent_b_incidental():
    values = reconcile_segments(
        pieces(rows(count=3, step=240), ["a", "b", "a"]), merge_keys=False
    )
    values[1]["evidence"][0]["end_at"] = "2026-08-01T08:08:00+00:00"
    g, desc = grounded(values)
    g["topic_purity"]["source_roles"][1]["role"] = "INCIDENTAL"
    with pytest.raises(ValueError, match="persistent"):
        validate_groups([g], values, {}, desc, require_purity=True)


def states(count=8):
    result = []
    for i in range(count):
        payload = semantic_payload(
            reconcile_segments(pieces(rows(count=1, offset=i), [str(i)]))[0],
            "anonymous-day",
            "UTC",
        )
        payload["reconciliation"]["information_value"] = 70
        result.append(
            SimpleNamespace(
                event_id=f"event-{i}",
                payload=payload,
                revision=1,
                status=SimpleNamespace(value="active"),
                derivation_status="active",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
        )
    return result


def test_information_value_prioritizes_arrangements_over_routine_without_changing_importance():
    events = states(2)
    events[0].payload["reconciliation"].update(
        information_value=90, routine_logistics=True
    )
    events[1].payload["reconciliation"].update(
        information_value=80, routine_logistics=False
    )
    value = structured_summary(events, (), "anonymous-day")
    assert [e["event_ids"] for e in value["major_events"]] == [["event-1"]]
    assert all(e.payload["importance"] == "MEDIUM" for e in events)


def test_specific_activity_signal_can_outrank_abstract_talk_without_changing_importance():
    events = states(2)
    events[0].payload["reconciliation"].update(information_value=70)
    events[1].payload["reconciliation"].update(
        information_value=60,
        materialization={"materialize": True, "reason": "significant_activity"},
    )
    value = structured_summary(events, (), "anonymous-day")
    assert value["major_events"][0]["event_ids"] == ["event-1"]
    assert all(e.payload["importance"] == "MEDIUM" for e in events)


@pytest.mark.parametrize("count", [3, 8])
def test_structured_overview_combines_main_lines_with_sources(count):
    events = states(count)
    ids = [e.event_id for e in events]
    content = {
        "headline": "项目评审与后续学习安排",
        "headline_source_event_ids": ids,
        "overview_sentences": [
            {
                "text": "当天围绕设备项目开展评审，讨论了验证路径和后续安排。",
                "source_event_ids": ids[:3],
            },
            {
                "text": "其余交流涉及学习和生活安排，具体事项保留在事件卡片中。",
                "source_event_ids": ids[2:],
            },
        ],
    }
    value = structured_summary(
        events,
        (),
        "anonymous-day",
        synthesis={"result": content, "provenance": {"remote": False}},
    )
    assert len(value["overview_claims"]) == 2
    assert not any(e.payload["summary"] in value["overview"] for e in events)
    assert all(c["event_ids"] for c in value["overview_claims"])


@pytest.mark.parametrize(
    "mutation", ["missing_ref", "unknown_ref", "multi_sentence", "too_many"]
)
def test_invalid_overview_citations_and_card_concatenation_rejected(mutation):
    content = {
        "headline": "匿名项目",
        "headline_source_event_ids": ["a"],
        "overview_sentences": [
            {"text": "当天讨论匿名项目。", "source_event_ids": ["a"]}
        ],
    }
    if mutation == "missing_ref":
        content["overview_sentences"][0]["source_event_ids"] = []
    elif mutation == "unknown_ref":
        content["overview_sentences"][0]["source_event_ids"] = ["invented"]
    elif mutation == "multi_sentence":
        content["overview_sentences"][0]["text"] = "卡片一。卡片二。"
    else:
        content["overview_sentences"] *= 5
    with pytest.raises(ValueError):
        validate_overview(content, {"a"})


def test_overview_cache_retry_sync_and_atomic_failure(source):
    class Analyzer(AnonymousAnalyzer):
        overview_prompt_version = "anonymous-overview.1"
        overview_schema_version = "anonymous-overview-schema.1"
        overview_calls = 0

        def overview(self, request):
            self.overview_calls += 1
            eid = request["major_events"][0]["event_id"]
            assert "utterances" not in request and "evidence" not in str(request.keys())
            return DailyOverviewResult(
                {
                    "headline": "匿名目标验证安排",
                    "headline_source_event_ids": [eid],
                    "overview_sentences": [
                        {
                            "text": "当天围绕匿名目标讨论验证安排。",
                            "source_event_ids": [eid],
                        }
                    ],
                },
                {"remote": False},
            )

    analyzer = Analyzer()
    source[1]._daily_analyzer = analyzer
    first = refresh(source)
    second = refresh(source)
    assert first == second and analyzer.overview_calls == 1
    assert (
        source[1].refresh_daily("2026-09-01", allow_model=False)["objective"] == second
    )

    class Broken(Analyzer):
        overview_prompt_version = "anonymous-overview.bad"

        def overview(self, request):
            raise ValueError("invalid structured output")

    source[1]._daily_analyzer = Broken()
    assert source[1].refresh_daily("2026-09-01")["semantic_status"] == "retryable"
    with source[2]().reading() as uow:
        assert (
            uow.knowledge.get_event(first["events"][0]["event_id"]).revision
            == first["events"][0]["revision"]
        )
