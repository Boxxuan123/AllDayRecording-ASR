"""Anonymous patterns from review feedback, not private transcripts/IDs/dates."""

from datetime import datetime, timedelta, timezone
import json
import pytest
from allday_asr.v3.domain.daily_semantics import (
    micro_batches,
    validate_segments,
    reconcile_segments,
    semantic_payload,
)
from allday_asr.v3.ports.daily_semantics import DailySemanticResult
from tests import test_daily_event_layer as fixtures

source = fixtures.source
add, revise, refresh = fixtures.add, fixtures.revise, fixtures.refresh


def rows(topic="A", count=36, offset=0, step=60):
    base = datetime(2026, 8, 1, 8, tzinfo=timezone.utc)
    return [
        {
            "utterance_id": f"{offset + i + 1:026d}",
            "revision": 1,
            "session_id": "anonymous-session",
            "start_at": (base + timedelta(seconds=(offset + i) * step)).isoformat(),
            "end_at": (base + timedelta(seconds=(offset + i) * step + 10)).isoformat(),
            "start_ms": (offset + i) * step * 1000,
            "end_ms": ((offset + i) * step + 10) * 1000,
            "text": f"[{topic}]匿名项目需要验证当前设计。",
            "audio_ranges": [],
            "participant": {
                "key": "unknown-track",
                "kind": "unknown",
                "label": "未知说话人",
            },
            "task_ids": [],
        }
        for i in range(count)
    ]


def segment(lo, hi, key="new:0", importance="MEDIUM", classification="CORE"):
    return {
        "start_index": lo,
        "end_index": hi,
        "event_key": key,
        "classification": classification,
        "core_topic": "匿名目标",
        "event_type": "conversation",
        "title": "讨论匿名目标的验证安排",
        "title_evidence_indices": [lo],
        "claims": [{"text": "录音讨论匿名目标的验证安排。", "evidence_indices": [lo]}],
        "importance": importance,
        "importance_reason": "meaningful goal",
        "boundary_reason": "persistent_topic_shift",
        "explicit_outcome": None,
        "confidence": 0.8,
    }


def pieces(source_rows, keys):
    return [
        segment(0, 0, key)
        | {
            "key": key,
            "evidence": [row],
            "title_evidence_utterance_ids": [row["utterance_id"]],
            "claims": [
                {
                    "text": "讨论验证安排。",
                    "evidence_utterance_ids": [row["utterance_id"]],
                }
            ],
        }
        for row, key in zip(source_rows, keys, strict=True)
    ]


def test_one_goal_spans_six_computation_windows_without_duration_cap():
    data = rows()
    assert len(micro_batches(data)) >= 6
    result = reconcile_segments(pieces(data, ["goal-a"] * len(data)))
    assert len(result) == 1 and len(result[0]["evidence"]) == 36
    assert (
        semantic_payload(result[0], "anonymous-day", "UTC")["reconciliation"][
            "final_duration_limit"
        ]
        is None
    )


def test_thirty_second_dining_detour_does_not_break_project():
    data = rows(count=5, step=10)
    candidates = pieces(data, ["project", "project", "dining", "project", "project"])
    candidates[2]["importance"] = "LOW"
    result = reconcile_segments(candidates)
    assert len(result) == 1 and data[2]["utterance_id"] not in {
        r["utterance_id"] for r in result[0]["evidence"]
    }


def test_sustained_new_goal_splits():
    data = rows(count=12)
    assert (
        len(
            reconcile_segments(
                pieces(data, ["competition"] * 6 + ["different-lab-goal"] * 6)
            )
        )
        == 2
    )


def test_three_persistent_topics_within_one_window_split():
    data = rows(count=9, step=20)
    assert len(micro_batches(data)) == 1
    assert len(reconcile_segments(pieces(data, ["a"] * 3 + ["b"] * 3 + ["c"] * 3))) == 3


def test_same_participants_do_not_merge_unrelated_goals():
    assert len(reconcile_segments(pieces(rows(count=2), ["a", "b"]))) == 2


def test_same_topic_hours_later_is_a_new_event():
    data = rows(count=2, step=3 * 3600)
    assert len(reconcile_segments(pieces(data, ["a", "a"]))) == 2


def test_activity_continues_across_windows():
    candidates = pieces(rows(count=20), ["activity"] * 20)
    for c in candidates:
        c["event_type"] = "activity"
    result = reconcile_segments(candidates)
    assert (
        len(result) == 1
        and semantic_payload(result[0], "anonymous-day", "UTC")["category"]
        == "activity"
    )


def test_fillers_do_not_materialize():
    data = rows(count=3)
    for r in data:
        r["text"] = "嗯嗯"
    assert not reconcile_segments(pieces(data, ["a"] * 3))


def test_routine_dining_secondary_despite_duration_and_speakers():
    candidates = pieces(rows(count=30), ["dining"] * 30)
    for c in candidates:
        c["importance"] = "LOW"
    p = semantic_payload(reconcile_segments(candidates)[0], "anonymous-day", "UTC")
    assert p["summary_visibility"] == "secondary" and p["salience"] == 0


def test_authoritative_task_boosts_salience():
    data = rows(count=1)
    data[0]["task_ids"] = ["authoritative-task"]
    c = pieces(data, ["a"])
    c[0]["importance"] = "LOW"
    p = semantic_payload(reconcile_segments(c)[0], "anonymous-day", "UTC")
    assert p["importance"] == "HIGH" and p["linked_task_ids"] == ["authoritative-task"]


def test_explicit_supported_outcome_outranks_discussion():
    candidates = pieces(rows(count=1), ["a"])
    plain = semantic_payload(reconcile_segments(candidates)[0], "anonymous-day", "UTC")
    candidates[0]["explicit_outcome"] = {
        "kind": "decision",
        "quote": "已经决定",
        "evidence_utterance_ids": [candidates[0]["evidence"][0]["utterance_id"]],
    }
    decided = semantic_payload(
        reconcile_segments(candidates)[0], "anonymous-day", "UTC"
    )
    assert decided["salience"] > plain["salience"]


def test_major_selection_does_not_cap_high_importance_rich_days():
    from allday_asr.v3.application.daily_structured_summary import structured_summary
    from types import SimpleNamespace

    events = []
    for i in range(10):
        p = semantic_payload(
            reconcile_segments(pieces(rows(count=1, offset=i), [str(i)]))[0],
            "anonymous-day",
            "UTC",
        )
        p["importance"] = "HIGH"
        events.append(
            SimpleNamespace(
                payload=p,
                event_id=str(i),
                revision=1,
                status=SimpleNamespace(value="active"),
                derivation_status="active",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
        )
    assert len(structured_summary(events, (), "anonymous-day")["major_events"]) == 10


def test_explicit_decision_requires_literal_source_quote():
    data = rows(count=1)
    data[0]["text"] = "已经决定下周验证匿名项目。"
    s = segment(0, 0)
    s["explicit_outcome"] = {
        "kind": "decision",
        "quote": data[0]["text"],
        "evidence_indices": [0],
    }
    assert validate_segments([s], data, set())[0]["explicit_outcome"]
    s["explicit_outcome"]["quote"] = "编造的完成结果"
    assert validate_segments([s], data, set())[0]["explicit_outcome"] is None


@pytest.mark.parametrize(
    "text", ["要不要下周验证？", "建议下周验证", "如果下周有空就验证"]
)
def test_question_suggestion_conditional_are_not_decisions(text):
    data = rows(count=1)
    data[0]["text"] = text
    s = segment(0, 0)
    s["explicit_outcome"] = {"kind": "decision", "quote": text, "evidence_indices": [0]}
    assert validate_segments([s], data, set())[0]["explicit_outcome"] is None


def test_noisy_source_can_have_cautious_semantic_title_not_raw_excerpt():
    data = rows(count=1)
    data[0]["text"] = "呃那个那个模模你说要嗯"
    s = segment(0, 0)
    s["title"] = "待确认的项目讨论"
    assert validate_segments([s], data, set())[0]["title"] == s["title"]
    s["title"] = "讨论片段：" + data[0]["text"]
    with pytest.raises(ValueError):
        validate_segments([s], data, set())


def test_unknown_speaker_is_retained_without_generated_name():
    p = semantic_payload(
        reconcile_segments(pieces(rows(count=1), ["a"]))[0], "anonymous-day", "UTC"
    )
    assert (
        p["participants"][0]["kind"] == "unknown"
        and "person_id" not in p["participants"][0]
    )


def test_evidence_claims_and_title_indices_stay_inside_segment():
    data = rows(count=2)
    s = segment(0, 1)
    s["claims"][0]["evidence_indices"] = [2]
    with pytest.raises(ValueError):
        validate_segments([s], data, set())


@pytest.mark.parametrize("classification", ["INCIDENTAL", "FILLER"])
def test_noncore_citations_are_validated_before_caching(classification):
    data = rows(count=2)
    value = segment(0, 1)
    value["classification"] = classification
    value["title_evidence_indices"] = [2]
    with pytest.raises(ValueError, match="outside its segment"):
        validate_segments([value], data, set())


def test_current_batch_cannot_omit_or_duplicate_sources():
    with pytest.raises(ValueError):
        validate_segments([segment(0, 0)], rows(count=2), set())
    with pytest.raises(ValueError):
        validate_segments([segment(0, 0), segment(0, 1)], rows(count=2), set())


class AnonymousAnalyzer:
    provider = "synthetic"
    model_label = "anonymous-model-double"
    prompt_version = "anonymous.1"
    producer_version = "test"
    calls = 0

    def analyze(self, request):
        self.calls += 1
        key = (
            request["open_context"][0]["event_key"]
            if request["open_context"]
            else "new:0"
        )
        return DailySemanticResult(
            (segment(0, len(request["utterances"]) - 1, key),),
            {
                "provider": "synthetic",
                "model": "anonymous-model-double",
                "remote": False,
            },
        )


def test_semantic_cache_replay_identity_and_claim_chain(source):
    analyzer = AnonymousAnalyzer()
    source[1]._daily_analyzer = analyzer
    add(source, text="匿名项目验证安排")
    first = refresh(source)
    second = refresh(source)
    assert (
        analyzer.calls == 1
        and first["events"][0]["event_id"] == second["events"][0]["event_id"]
    )
    assert first["major_events"] and first["overview"]
    event = first["events"][0]
    ids = {r["utterance_id"] for r in event["evidence_snapshots"]}
    assert all(set(c["evidence_utterance_ids"]) <= ids for c in event["summary_claims"])
    assert all(
        set(c["event_ids"]) <= set(first["source_event_ids"])
        for c in first["overview_claims"]
    )


def test_semantic_revision_and_late_source_reconcile(source):
    source[1]._daily_analyzer = AnonymousAnalyzer()
    first = refresh(source)["events"][0]
    revise(source, text="修正后的匿名项目安排")
    second = refresh(source)["events"][0]
    assert (
        second["revision"] == first["revision"] + 1
        and second["event_id"] == first["event_id"]
    )
    add(source, text="迟到的匿名项目证据")
    third = refresh(source)["events"][0]
    assert (
        third["event_id"] == first["event_id"]
        and third["revision"] == second["revision"] + 1
    )


def test_invalid_legacy_cache_is_retryable_without_sync_model_call(source):
    analyzer = AnonymousAnalyzer()
    source[1]._daily_analyzer = analyzer
    first = refresh(source)
    with source[0].database.transaction() as c:
        row = c.execute(
            "SELECT generation_id,input_scope_json FROM generation_records WHERE producer='daily-semantic'"
        ).fetchone()
        scope = json.loads(row["input_scope_json"])
        scope["result"][0]["title_evidence_indices"] = [999]
        c.execute(
            "UPDATE generation_records SET input_scope_json=? WHERE generation_id=?",
            (json.dumps(scope), row["generation_id"]),
        )
    assert (
        source[1].refresh_daily("2026-09-01", allow_model=False)["semantic_status"]
        == "pending"
    )
    assert analyzer.calls == 1
    second = refresh(source)
    assert (
        analyzer.calls == 2
        and first["events"][0]["event_id"] == second["events"][0]["event_id"]
    )
    refresh(source)
    assert analyzer.calls == 2


def test_source_revision_during_inference_preserves_published_events(source):
    first = refresh(source)

    class ConcurrentRevision(AnonymousAnalyzer):
        def analyze(self, request):
            result = super().analyze(request)
            revise(source, text="并发修订的匿名项目材料")
            return result

    source[1]._daily_analyzer = ConcurrentRevision()
    result = source[1].refresh_daily("2026-09-01")
    assert result == {"semantic_status": "retryable", "error": "daily_sources_changed"}
    with source[2]().reading() as uow:
        assert uow.knowledge.get_event(first["events"][0]["event_id"]).revision == 1


def test_model_failure_leaves_published_events_unchanged(source):
    first = refresh(source)

    class Broken(AnonymousAnalyzer):
        def analyze(self, request):
            raise RuntimeError("synthetic transport failure")

    source[1]._daily_analyzer = Broken()
    assert source[1].refresh_daily("2026-09-01")["semantic_status"] == "retryable"
    with source[2]().reading() as uow:
        assert uow.knowledge.get_event(first["events"][0]["event_id"]).revision == 1


def test_sync_does_not_invoke_remote_model(source):
    analyzer = AnonymousAnalyzer()
    source[1]._daily_analyzer = analyzer
    result = source[1].refresh_daily("2026-09-01", allow_model=False)
    assert result["semantic_status"] == "pending" and analyzer.calls == 0


def test_bad_source_indices_retry_without_repairing_model_claims(source):
    class RetryAnalyzer(AnonymousAnalyzer):
        def analyze(self, request):
            result = super().analyze(request)
            if self.calls < 3:
                result.segments[0]["claims"][0]["evidence_indices"] = [999]
            return result

    analyzer = RetryAnalyzer()
    source[1]._daily_analyzer = analyzer
    result = refresh(source)
    assert analyzer.calls == 3 and result["semantic_status"] == "complete"
    assert result["events"][0]["semantic_provenance"]["attempts"] == 3


def test_service_closes_both_generators(source):
    class Closeable:
        closed = False

        def close(self):
            self.closed = True

    generator, analyzer = Closeable(), Closeable()
    source[1]._generator = generator
    source[1]._daily_analyzer = analyzer
    source[1].close()
    assert generator.closed and analyzer.closed


def test_split_with_reordered_anchor_preserves_unique_ids_and_replay(source):
    uid = add(source, text="匿名项目还有新的验证材料")
    baseline = refresh(source)
    assert len(baseline["events"]) == 1
    seed = source[0]
    with seed.database.transaction() as c:
        c.execute(
            "UPDATE utterances SET start_at=?,end_at=?,revision=revision+1 WHERE utterance_id=?",
            (
                (seed.now + timedelta(milliseconds=50)).isoformat(),
                (seed.now + timedelta(milliseconds=900)).isoformat(),
                uid,
            ),
        )

    class SplitAnalyzer(AnonymousAnalyzer):
        def analyze(self, request):
            self.calls += 1
            return DailySemanticResult(
                (
                    segment(0, 0, "new:0"),
                    segment(1, len(request["utterances"]) - 1, "new:1"),
                ),
                {},
            )

    source[1]._daily_analyzer = SplitAnalyzer()
    first = source[1].refresh_daily("2026-09-01")
    second = source[1].refresh_daily("2026-09-01")
    ids = [e["event_id"] for e in first["objective"]["events"]]
    assert len(ids) == len(set(ids)) == 2
    assert second["revision"] == first["revision"]
