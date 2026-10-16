"""Fictional contract regressions for normal product requests; no private answers."""

import copy
from datetime import datetime
import pytest
from tests.test_recall_contacts import native as fixture
from tests.test_recall_query_loop import ask
from allday_asr.v3.bootstrap.history_recall import compose_history_recall
from allday_asr.v3.domain.recall_query import understand
from allday_asr.v3.domain.recall_address import direct_address
from allday_asr.v3.domain.recall_temporal import nearby_scope
from allday_asr.v3.adapters.sqlite.recall_preferences import RecallPreferences
from allday_asr.v3.adapters.codex.investigation_gateway import InvestigationGateway
from tests.test_investigation_capabilities import request, sse, post


@pytest.fixture
def env(tmp_path, monkeypatch):
    return fixture.__wrapped__(tmp_path, monkeypatch)


def test_explicit_preference_reaches_real_retrieval_after_service_restart(env):
    r = ask(env, "昨天我和青岚聊了什么", expand=True)
    c = next(c for c in r["contact_candidates"] if c["platform"] == "wechat")
    # Legacy choose is still transient.
    transient = env.service.execute(
        "phone",
        {"action": "choose", "query_id": r["query_id"], "contact_key": c["key"]},
    )
    assert "target_preference" not in transient
    assert (
        ask(env, "昨天我和青岚聊了什么", expand=True)["status"] == "needs_clarification"
    )
    # Explicit new confirmation changes only query preference state.
    remembered = env.service.execute(
        "phone", {"action": "remember_target", "query_id": transient["query_id"]}
    )
    assert remembered["target_preference"]["platform"] == "wechat"
    env.service = compose_history_recall(
        env.db, env.service.root, env.source, env.service.model
    )
    repeated = ask(env, "昨天我和青岚聊了什么", expand=True)
    assert repeated["model_status"] == "available"
    assert repeated["scope"]["source"] == "wechat"
    assert repeated["scope"]["sent_from"] == transient["scope"]["sent_from"]
    assert {e["conversation_id"] for e in repeated["evidence"]} == {"native-wechat"}
    assert {e["effective_is_self"] for e in repeated["evidence"]} == {False, True}
    assert (
        env.service.preferences.resolve(
            "stranger", "青岚", "all", env.service.directory.cached()
        )
        is None
    )
    env.service.execute(
        "phone", {"action": "forget_target", "query_id": repeated["query_id"]}
    )
    assert (
        ask(env, "昨天我和青岚聊了什么", expand=True)["status"] == "needs_clarification"
    )


def test_preference_cannot_cross_dataset_or_explicit_platform(tmp_path):
    prefs = RecallPreferences(tmp_path)
    c = dict(key="one", dataset="A", display_name="Cedar", platform="qq")
    prefs.remember("owner", "Cedar", c)
    assert prefs.resolve("owner", "Cedar", "wechat", [c]) is None
    assert prefs.resolve("owner", "Cedar", "all", [{**c, "dataset": "B"}]) is None


def test_short_name_does_not_capture_topic_identifier():
    book = {
        "native:c": dict(
            person_id="native:c", display_name="c", aliases=[], kind="contact"
        )
    }
    now = int(datetime.fromisoformat("2026-08-20T08:35:00+08:00").timestamp())
    r = understand("前天晚上KCP发生了什么", "", book, {}, now)
    assert r["person_mention"] == "" and r["scope"]["person_id"] == ""
    assert r["terms"] == ["KCP"]


def test_direct_address_requires_direction_peer_context_and_no_conflict():
    c = dict(
        platform="qq",
        source_account_id="A",
        conversation_id="room",
        conversation_type="c2c",
        key="K",
    )
    base = dict(
        source="qq",
        source_account_id="A",
        conversation_id="room",
        conversation_type="c2c",
    )
    rows = [
        {
            **base,
            "id": "one",
            "quote": "老李，您之前的资料还有吗",
            "source_is_from_self": True,
            "sent_at": 100,
        },
        {
            **base,
            "id": "two",
            "quote": "有，我找一下",
            "source_is_from_self": False,
            "sender_account_id": "peer",
            "sent_at": 200,
        },
    ]
    assert direct_address(rows, "老李", [c])[0]["sender_account_id"] == "peer"
    for change in [
        dict(source_is_from_self=False),
        dict(quote="他提到了老李"),
        dict(conversation_type="group"),
    ]:
        bad = copy.deepcopy(rows)
        bad[0].update(change)
        assert direct_address(bad, "老李", [c]) is None
    bad = copy.deepcopy(rows)
    bad[1]["quote"] = "你认错了，我不是老李"
    assert direct_address(bad, "老李", [c]) is None
    other = [{**r, "conversation_id": "other"} for r in rows]
    assert (
        direct_address(rows + other, "老李", [c, {**c, "conversation_id": "other"}])
        is None
    )


def test_natural_date_tolerance_is_one_day_and_hard_dates_win():
    first = int(datetime.fromisoformat("2026-08-18T18:00:00+08:00").timestamp())
    scope = dict(
        timezone="Asia/Singapore", sent_from=first, sent_to=first + 6 * 3600 - 1
    )
    candidate = nearby_scope(scope, {"question": "前天晚上观测社发生了什么"})
    assert candidate["sent_to"] == first - 18 * 3600 - 1
    assert candidate["sent_to"] - candidate["sent_from"] + 1 == 86400
    assert nearby_scope(scope, {"question": "前天晚上", "sent_from": first}) is None
    assert nearby_scope(scope, {"question": "2026年8月18日发生了什么"}) is None


def test_observational_metadata_is_dropped_but_tools_still_blocked(tmp_path):
    def upstream(*args):
        return sse(
            {"type": "response.metadata", "metadata": {"trace": "opaque"}},
            {"type": "response.completed", "response": {"output": [], "tools": []}},
        )

    with InvestigationGateway(
        request(), tmp_path / "ok.json", transport=upstream
    ) as gate:
        result = post(gate, {"model": "gpt-6.1-sol"})
    assert b"response.metadata" not in result and b"response.completed" in result
    assert gate.state["ignored_metadata_events"] == 1 and gate.state["completed"]

    def hostile(*args):
        return sse({"type": "response.metadata", "item": {"type": "function_call"}})

    with InvestigationGateway(
        request(), tmp_path / "bad.json", transport=hostile
    ) as gate:
        assert post(gate, {"model": "gpt-6.1-sol"}) == b""
    assert gate.state["blocked_events"] == 1
