"""Night grammar and cold-date Mac retrieval: fictional data, no live services."""

import pytest
from datetime import datetime
from tests.test_recall_query_loop import env as fixture, ask, message, DAY, NOW, SUMMARY
from tests.test_history_recall import dump
from allday_asr.v3.domain.recall_query import understand, dates


@pytest.fixture
def env(tmp_path, monkeypatch):
    return fixture.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.parametrize(
    "question",
    [
        "昨天我跟蓝老师聊了什么",
        "昨晚我跟蓝老师聊了什么",
        "我昨晚跟蓝老师聊了什么",
        "昨天我们和蓝老师聊了什么",
        "昨天我和蓝老师说了啥",
        "昨天晚上我们跟蓝老师谈了什么",
        "我们昨晚和Aster聊了什么",
        "前天我跟Aster谈了什么",
    ],
)
def test_generic_grammar(env, question):
    p = understand(question, "", env.service.identities.people(), {}, NOW + 9 * 3600)
    assert (
        p["scope"]["person_id"] == "alice-person"
        and p["scope"]["relationship"] == "conversation"
    )
    assert p["intent"] == "conversation" and p["terms"] == [] and p["issue"] == ""
    assert p["time_explicit"]


@pytest.mark.parametrize(
    "question", ["我昨晚跟蓝老师后来如何", "上周我们和蓝老师后来如何"]
)
def test_followup_keeps_relationship_and_latest(env, question):
    p = understand(question, "", env.service.identities.people(), {}, NOW + 9 * 3600)
    assert (
        p["scope"]["person_id"] == "alice-person"
        and p["scope"]["relationship"] == "conversation"
    )
    assert p["intent"] == "latest" and p["terms"] == []


@pytest.mark.parametrize(
    "now,expected",
    [
        ("2027-01-01T09:00:00+08:00", "2026-12-31T18:00:00+08:00"),
        ("2026-03-01T09:00:00+08:00", "2026-02-28T18:00:00+08:00"),
    ],
)
def test_night_spans_midnight(now, expected):
    stamp = int(datetime.fromisoformat(now).timestamp())
    for text in ["昨晚", "昨天晚上"]:
        period, _ = dates(text, "Asia/Singapore", stamp)
        assert period[0] == int(datetime.fromisoformat(expected).timestamp())
        assert period[1] - period[0] + 1 == 12 * 3600
    period, _ = dates("昨晚", "Asia/Singapore", stamp - 6 * 3600)
    assert period[1] == stamp - 6 * 3600


@pytest.mark.parametrize("word", ["前晚", "昨夜", "刚才", "去年", "上个月"])
def test_unresolved_time_never_becomes_default_window(env, word):
    with pytest.raises(ValueError, match="时间描述"):
        ask(env, f"我{word}跟蓝老师聊了什么")
    assert env.source.calls == []


@pytest.fixture
def cold(env):
    # Keep only old stable conversation/identity metadata in Windows, and put
    # the query-day rows exclusively in the fake Mac source.
    with env.cache.connect() as c:
        c.execute("UPDATE search_index SET sent_at=sent_at-86400*7")
    env.source.records = [
        message(2000, "请把样品检查安排在周五。"),
        message(2001, "周五十点可以，我来准备清单。", "self-account"),
    ]
    return env


def test_cold_date_fetches_both_sides_without_cache_or_business_writes(cold):
    before = dump(cold.db.path), dump(cold.cache.path)
    result = ask(cold, expand=True)
    assert result["answer"] == SUMMARY and result["model_status"] == "available"
    assert {r["record_id"] for r in result["evidence"]} == {
        "rec:fixture-2000",
        "rec:fixture-2001",
    }
    assert all(r["origin"] == "mac" for r in result["evidence"])
    calls = [v["scope"] for route, b, v in cold.source.calls if route == "/v1/messages"]
    assert calls and all(
        v["sent_from"] == DAY and v["sent_to"] == DAY + 86400 - 1 for v in calls
    )
    assert all("conversation_id" in v and "sender_account_id" not in v for v in calls)
    assert (dump(cold.db.path), dump(cold.cache.path)) == before
    assert result["scope"]["sent_from_local"].startswith("2026-10-13T00:00:00")


@pytest.mark.parametrize("platform", ["qq", "wechat"])
def test_old_stable_platform_scope_fetches_new_day(cold, platform):
    from tests.test_history_recall import decision

    old = message(
        2100,
        "旧日会话身份",
        platform=platform,
        conversation="stable-platform",
        sent=DAY - 86400 * 9,
    )
    cold.cache.receive(cold.source.dataset, [old])
    cold.cache.process(100)
    decision(cold.service, old)
    cold.source.records = [
        message(
            2101,
            "请把样品检查安排在周五。",
            platform=platform,
            conversation="stable-platform",
        ),
        message(
            2102,
            "周五十点可以，我来准备清单。",
            "self-account",
            platform,
            "stable-platform",
        ),
    ]
    result = ask(cold, source=platform, expand=True)
    assert result["answer"] == SUMMARY
    assert {r["source"] for r in result["evidence"]} == {platform}
    assert {r["person_id"] for r in result["evidence"]} == {"alice-person", "myself"}


@pytest.mark.parametrize("mode", ["empty", "offline", "collecting", "partial"])
def test_unavailable_or_incomplete_mac_is_coverage_insufficient(cold, mode):
    original = cold.source.request
    if mode == "empty":
        cold.source.records = []
    if mode == "offline":
        cold.source.failure = "SOURCE_OFFLINE"
    if mode == "collecting":

        def request(route, body=None, **params):
            value = original(route, body, **params)
            if route == "/v1/status":
                value["status"] = "collecting"
            return value

        cold.source.request = request
    if mode == "partial":
        cold.source.records = cold.source.records[:1]

        def request(route, body=None, **params):
            value = original(route, body, **params)
            if route == "/v1/messages":
                value.update(has_more=True, next_cursor=None)
            return value

        cold.source.request = request
    result = ask(cold, expand=True)
    assert result["status"] == "coverage_insufficient" and result["claims"] == []
    assert "覆盖不足" in result["answer"] and "不能判断没有聊天" in result["answer"]
    assert result["metrics"]["model_calls"] == 0


def test_pagination_brings_self_reply_into_correct_context(cold):
    cold.source.records = [message(2200 + i, "请核对样品检查。") for i in range(40)]
    cold.source.records.append(
        message(2250, "周五十点可以，我来准备清单。", "self-account")
    )
    result = ask(cold, expand=True)
    assert result["answer"] == SUMMARY and result["metrics"]["source_requests"] <= 6
    assert result["metrics"]["source_records_read"] <= 100
    assert any(
        r["effective_is_self"] is True
        for r in cold.service._saved("phone", result["query_id"])["evidence"]
    )
    assert any(
        "cursor" in p for route, b, p in cold.source.calls if route == "/v1/messages"
    )


def test_wrong_date_namespace_group_and_person_are_rejected(cold):
    original = cold.source.request
    wrong = message(
        2300,
        "无关人物",
        sender="bob",
        conversation="unknown-group",
        kind="group",
        sent=DAY - 1,
    )

    def request(route, body=None, **params):
        if route == "/v1/messages":
            return {"items": [wrong], "has_more": False}
        return original(route, body, **params)

    cold.source.request = request
    result = ask(cold, expand=True)
    assert result["evidence"] == [] and result["claims"] == []
    assert result["status"] == "coverage_insufficient"
    assert "无关人物" not in result["answer"]


def test_unknown_alias_makes_zero_mac_requests(cold):
    result = ask(cold, "我昨晚跟不存在的老师聊了什么", expand=True)
    assert result["status"] == "needs_clarification" and result["evidence"] == []
    assert cold.source.calls == []


@pytest.mark.parametrize(
    "question", ["昨晚八点我跟蓝老师聊了什么", "昨晚20点我跟蓝老师聊了什么"]
)
def test_unresolved_clock_is_not_ignored(env, question):
    with pytest.raises(ValueError, match="时间描述"):
        ask(env, question)
    assert env.source.calls == []


def test_night_remote_range_includes_midnight_but_not_daytime(cold):
    moment = datetime.fromisoformat("2026-10-14T09:00:00+08:00")
    clock = int(moment.timestamp())
    from unittest.mock import patch

    start = DAY + 18 * 3600
    cold.source.records = [
        message(2400, "请把样品检查安排在周五。", sent=start + 3600),
        message(
            2401,
            "周五十点可以，我来准备清单。",
            "self-account",
            sent=DAY + 86400 + 3600,
        ),
        message(2402, "白天不属于昨晚", sent=start - 1),
        message(2403, "早晨不属于昨晚", sent=DAY + 86400 + 6 * 3600),
        message(
            2404,
            "错误人物在其他会话",
            sender="bob",
            conversation="private-b",
            sent=start + 100,
        ),
    ]
    with patch(
        "allday_asr.v3.application.history_recall.time.time", return_value=clock
    ):
        result = ask(cold, "我昨晚跟蓝老师聊了什么", expand=True)
    assert result["answer"] == SUMMARY
    assert {r["record_id"] for r in result["evidence"]} == {
        "rec:fixture-2400",
        "rec:fixture-2401",
    }
    assert result["scope"]["sent_from"] == start
    assert result["scope"]["sent_to"] == DAY + 86400 + 6 * 3600 - 1


def test_later_source_failure_retains_only_valid_bilateral_originals(cold):
    from allday_asr.v3.ports.chat_data import ChatDataError

    for row in cold.source.records:
        row["conversation_id"] = "private-second"
    original = cold.source.request
    number = 0

    def request(route, body=None, **params):
        nonlocal number
        if route == "/v1/messages":
            number += 1
            if number > 1:
                raise ChatDataError("SOURCE_OFFLINE", 503)
        return original(route, body, **params)

    cold.source.request = request
    result = ask(cold, expand=True, summarize=False)
    assert {r["record_id"] for r in result["evidence"]} == {
        "rec:fixture-2000",
        "rec:fixture-2001",
    }
    assert result["source_status"] == "unavailable" and result["claims"] == []
    assert "SOURCE_OFFLINE" in str(result["coverage"])


def test_historical_scopes_use_existing_namespace_index(cold):
    with cold.cache.connect() as c:
        plan = c.execute(
            "EXPLAIN QUERY PLAN SELECT conversation,max(sent_at) FROM search_index WHERE dataset=? AND platform=? AND source=? AND sender=? GROUP BY conversation LIMIT 501",
            (cold.source.dataset, "qq", "local-account", "alice"),
        ).fetchall()
    assert any("SEARCH search_index USING" in r["detail"] for r in plan)
    assert not any(r["detail"] == "SCAN search_index" for r in plan)
