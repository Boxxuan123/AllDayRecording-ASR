"""Actual transcript/projection and downstream contracts under unknown identity."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.test_blind_validation import world as world, seed
from tests.test_self_identity_regression import Provider, anchor, evidence
from tests import test_v33_intelligent_reminders as reminders_fixture
from tests import test_v36_daily_insights as insights_fixture
from tests import test_v35_cross_day_person_memory as memories_fixture
from tests import test_v34_open_speaker_identity as fixtures
from allday_asr.v3.adapters.transfer.speaker_continuity import infer_safely


def visible_session(world):
    sid = seed(world, "holdout")
    with world[1].database.transaction() as c:
        c.execute(
            "UPDATE utterances SET utterance_id=? WHERE utterance_id='extra-1'",
            (fixtures._utterance_id(9000),),
        )
    return sid


@pytest.mark.parametrize("fault", ["embedding", "match", "status"])
def test_speaker_failure_preserves_real_transcript_and_unknown_projection(world, fault):
    _, core, *_ = world
    sid = visible_session(world)
    evidence(core, sid)
    matcher, _ = anchor(core)
    core.people._provider = Provider()
    before = core.desktop.session_detail(sid)["utterances"]
    timeline_before = core.desktop.timeline_page(sid)

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic speaker failure")

    if fault == "embedding":
        core.people._provider.embed = fail
    elif fault == "status":
        matcher.status = fail
    else:
        matcher.match = fail
    result = core.people.analyze(sid)
    assert result["product_inference_executed"] and result["product_inference_degraded"]
    assert result["product_self_updated_utterance_count"] == 0
    after = core.desktop.session_detail(sid)["utterances"]
    assert [(u["utterance_id"], u["text"], u["identity"]) for u in after] == [
        (u["utterance_id"], u["text"], u["identity"]) for u in before
    ]
    assert all(u["identity"] == "unknown" for u in after)
    assert core.desktop.timeline_page(sid) == timeline_before
    with core.database.read() as c:
        assert c.execute("SELECT COUNT(*) FROM voice_prototypes").fetchone()[0] == 0


def test_workflow_fallback_does_not_hide_or_modify_durable_transcript(world):
    _, core, *_ = world
    sid = visible_session(world)
    before = core.desktop.session_detail(sid)["utterances"]

    def fail(session_id):
        raise RuntimeError("speaker subsystem unavailable")

    facade = SimpleNamespace(people=SimpleNamespace(analyze=fail))
    result = infer_safely(facade, sid)
    assert result["status"] == "degraded" and result["fallback_identity"] == "unknown"
    assert not result["automatic_person_attribution"]
    assert core.desktop.session_detail(sid)["utterances"] == before


def test_unknown_still_produces_pending_todo_without_self_attribution():
    h = reminders_fixture.V33IntelligentReminderTests()
    h.setUp()
    try:
        with h.database.transaction() as c:
            c.execute(
                "UPDATE utterances SET identity='unknown' WHERE utterance_id=?",
                (reminders_fixture.UTTERANCE_ID,),
            )
        candidate = h.reminders.submit_generation(
            reminders_fixture._submission(
                replace(
                    reminders_fixture._intent(
                        title="明天发送会议材料",
                        confidence=1.0,
                        needs_confirmation=False,
                        scheduled_at=h.current_time + timedelta(hours=12),
                    ),
                    actor_person_id="unresolved",
                    related_person_ids=(),
                )
            )
        )["candidates"][0]
        assert candidate["status"] == "pending_confirmation"
        assert candidate["actor_person_id"] == "unresolved"
    finally:
        h.tearDown()


def test_daily_summary_includes_anonymous_event():
    h = insights_fixture.V36DailyInsightTests()
    h.setUp()
    try:
        anon = h._event(
            2,
            "decision",
            {
                "title": "匿名说话人确认周五交付",
                "related_person_ids": [],
                "topics": ["交付"],
            },
        )
        result = h.insights.generate_daily("2026-09-01", "Asia/Singapore")
        request = h.generator.daily_requests[-1]
        event = next(e for e in request.source_events if e["event_id"] == anon)
        assert event["person_ids"] == []
        assert result["objective"]["statistics"]["decision_count"] == 2
    finally:
        h.tearDown()


def test_unresolved_event_is_excluded_from_person_relationship_memory():
    h = memories_fixture.V35CrossDayPersonMemoryTests()
    h.setUp()
    try:
        h._event(
            2,
            "commitment",
            {
                "title": "匿名说话人答应交付",
                "actor_person_id": "unresolved",
                "related_person_ids": [],
            },
        )
        assert h.memories.refresh("person-a")["created_count"] == 0
        assert h.memories.person("person-a")["memory_count"] == 0
    finally:
        h.tearDown()
