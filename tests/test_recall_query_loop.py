"""Fictional final-answer acceptance; never connects to production or a model."""

import json
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
import pytest
from tests.chat_fixture import record, FixtureMac
from tests.test_history_recall import env as original_env, decision, query, dump
from allday_asr.v3.adapters.sqlite.chat_cache import ChatCache
from allday_asr.v3.bootstrap.history_recall import compose_history_recall
from allday_asr.v3.domain.recall_query import dates, understand

NOW = int(datetime.fromisoformat("2026-10-14T00:01:00+08:00").timestamp())
DAY = int(datetime.fromisoformat("2026-10-13T00:00:00+08:00").timestamp())
SUMMARY = "你们讨论了样品检查，约定周五十点检查，由本人准备清单。"


def message(
    i,
    text,
    sender="alice",
    platform="qq",
    conversation="private-a",
    sent=None,
    kind="c2c",
    reply=None,
):
    r = record(
        i,
        text,
        platform,
        conversation,
        DAY + 9 * 3600 + i if sent is None else sent,
        sender,
    )
    r.update(
        conversation_type=kind,
        is_from_self=sender == "self-account",
        self_identity_basis="synthetic verified direction",
        reply=reply,
    )
    return r


def summary_model(payload, cancel):
    rows = payload["records"]
    target = next(r for r in rows if r["person_id"] == "alice-person")
    self_ = next(r for r in rows if r["effective_is_self"] is True)
    return {
        "claims": [
            {
                "kind": "summary",
                "text": SUMMARY,
                "citations": [target["id"], self_["id"]],
            }
        ],
        "unknown": ["仅本次取回的对话，其他会话和实际执行状态尚未确认。"],
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    old, _, db, _, _ = original_env.__wrapped__(tmp_path / "base")
    with db.transaction() as c:
        for pid, aliases in [
            ("alice-person", ["蓝老师", "Aster"]),
            ("bob-person", ["绿老师"]),
        ]:
            c.execute(
                "INSERT INTO person_profile_revisions SELECT person_id,revision+1,display_name,?,relationship_labels_json,notes,?,created_at FROM person_profile_revisions WHERE person_id=? ORDER BY revision DESC LIMIT 1",
                (json.dumps(aliases, ensure_ascii=False), "fictional-human", pid),
            )
    rows = [
        message(1001, "请把样品检查安排在周五。"),
        message(1002, "周五十点可以，我来准备清单。", "self-account"),
        message(1003, "明天我跟其他同事有约。", "bob", conversation="private-b"),
        message(1004, "我跟乙聊另外一件事。", "self-account", conversation="private-b"),
        message(1005, "错误日期的旧安排。", sent=DAY - 2 * 86400 - 1),
        message(1006, "错误日期本人。", "self-account", sent=DAY - 2 * 86400 - 2),
        message(1007, "未知账号旁观者。", "unresolved"),
        message(1008, "已完成清单复核。", conversation="private-second"),
        message(
            1009,
            "收到，之后等检查结果。",
            "self-account",
            conversation="private-second",
        ),
        message(
            1010, "另一个群的闲聊。", "bob", conversation="group-other", kind="group"
        ),
        message(1011, "群里检查安排请确认。", conversation="group-pair", kind="group"),
        message(
            1012,
            "群里我确认周五。",
            "self-account",
            conversation="group-pair",
            kind="group",
            reply={"record_id": "rec:fixture-1011"},
        ),
        message(
            1013,
            "其他成员转述：蓝老师已经全部完成。",
            "bob",
            conversation="group-pair",
            kind="group",
        ),
        message(
            1014,
            "没有回复关系的本人闲聊。",
            "self-account",
            conversation="group-pair",
            kind="group",
        ),
        message(
            1015, "没有回复关系的对方闲聊。", conversation="group-pair", kind="group"
        ),
    ]
    source = FixtureMac(rows)
    cache = ChatCache(tmp_path / "chat/cache.sqlite3")
    cache.activate(source.dataset)
    cache.receive(source.dataset, rows)
    cache.process(100)
    service = compose_history_recall(db, tmp_path, source, summary_model)
    decision(service, rows[0])
    decision(service, rows[2], person="bob-person")
    monkeypatch.setattr(
        "allday_asr.v3.application.history_recall.time.time", lambda: NOW
    )
    return SimpleNamespace(
        service=service, cache=cache, db=db, rows=rows, source=source
    )


def ask(env, question="昨天我跟蓝老师聊了什么", **extra):
    options = dict(
        question=question,
        keyword="",
        timezone="Asia/Singapore",
        sent_from=DAY - 86400,
        sent_to=DAY + 2 * 86400,
        source="all",
        summarize=True,
        expand=False,
    )
    options.update(extra)
    return query(env.service, **options)


def test_bilateral_summary_and_hard_constraints_reach_final_answer(env):
    before = dump(env.db.path), dump(env.cache.path)
    seen = []
    env.service.model = lambda p, c: seen.append(deepcopy(p)) or summary_model(p, c)
    result = ask(env)
    assert result["intent"] == "conversation" and result["terms"] == []
    assert result["scope"]["person_id"] == "alice-person"
    assert (result["scope"]["sent_from"], result["scope"]["sent_to"]) == (
        DAY,
        DAY + 86400 - 1,
    )
    ids = {r["record_id"] for r in result["evidence"]}
    assert ids == {
        "rec:fixture-1001",
        "rec:fixture-1002",
        "rec:fixture-1008",
        "rec:fixture-1009",
        "rec:fixture-1011",
        "rec:fixture-1012",
    }
    assert result["answer"] == SUMMARY and result["model_status"] == "available"
    assert result["claims"][0]["citations"] and result["unknown"]
    assert len({r.get("conversation_id") for r in seen[0]["records"]}) == 3
    assert any(r["effective_is_self"] is True for r in seen[0]["records"])
    assert all(r["person_id"] in {"alice-person", "myself"} for r in seen[0]["records"])
    assert "天我" not in result["terms"] and "我跟" not in result["terms"]
    assert (dump(env.db.path), dump(env.cache.path)) == before


@pytest.mark.parametrize("alias", ["甲同学", "蓝老师", "Aster"])
@pytest.mark.parametrize("platform", ["qq", "wechat"])
def test_alias_platform_and_time_variation(env, alias, platform):
    rows = [
        message(
            1100,
            "请把样品检查安排在周五。",
            platform=platform,
            conversation="new-platform",
            sent=DAY - 86400 + 1,
        ),
        message(
            1101,
            "周五十点可以，我来准备清单。",
            "self-account",
            platform,
            "new-platform",
            DAY - 86400 + 2,
        ),
    ]
    env.cache.receive(env.source.dataset, rows)
    env.cache.process(100)
    decision(env.service, rows[0])
    result = ask(
        env, f"前天我在{'QQ' if platform == 'qq' else '微信'}跟{alias}聊了什么"
    )
    assert result["answer"] == SUMMARY and {
        r["source"] for r in result["evidence"]
    } == {platform}
    assert {r["record_id"] for r in result["evidence"]} == {
        r["record_id"] for r in rows
    }


def test_unknown_alias_and_explicit_unknown_person_never_search_all(env):
    result = ask(env, "昨天我跟陌生老师聊了什么")
    assert result["status"] == "needs_clarification" and result["evidence"] == []
    assert "没有可信" in result["answer"] and result["metrics"]["model_calls"] == 0
    assert env.source.calls == []
    with pytest.raises(ValueError, match="尚未确认"):
        ask(env, person_id="invented-person")


def test_same_name_requires_selection_and_selection_is_checked(env):
    with env.db.transaction() as c:
        c.execute(
            "INSERT INTO person_profile_revisions SELECT person_id,revision+1,display_name,?,relationship_labels_json,notes,?,created_at FROM person_profile_revisions WHERE person_id=? ORDER BY revision DESC LIMIT 1",
            ('["蓝老师"]', "fictional-human", "bob-person"),
        )
    result = ask(env)
    assert (
        result["status"] == "needs_clarification"
        and len(result["person_candidates"]) == 2
    )
    assert result["evidence"] == [] and "选择" in result["answer"]
    assert ask(env, person_id="alice-person")["answer"] == SUMMARY
    conflict = ask(env, "昨天我跟Aster聊了什么", person_id="bob-person")
    assert conflict["status"] == "needs_clarification" and conflict["evidence"] == []


@pytest.mark.parametrize(
    "question",
    [
        "今天我跟蓝老师聊了什么",
        "昨天我跟绿老师聊了什么",
        "2026-10-12我跟蓝老师聊了什么",
    ],
)
def test_empty_and_negative_scope_does_not_masquerade_as_summary(env, question):
    if "绿老师" in question:
        env.service.model = lambda p, c: {
            "claims": [
                {
                    "kind": "quote",
                    "text": p["records"][0]["quote"],
                    "citations": [p["records"][0]["id"]],
                }
            ],
            "unknown": [],
        }
        result = ask(env, question)
        assert result["answer"] != "" and "周五十点" not in result["answer"]
        assert {r["person_id"] for r in result["evidence"]} == {"bob-person", "myself"}
    else:
        result = ask(env, question)
        assert result["status"] == "not_found" and result["claims"] == []
        assert "未找到" in result["answer"] and result["metrics"]["model_calls"] == 0


def test_correct_person_date_but_no_interaction_is_not_a_conversation(env):
    solitary = [message(1200, "这是对方单独发言。", sent=DAY + 86400 + 2)]
    env.cache.receive(env.source.dataset, solitary)
    env.cache.process(100)
    result = ask(env, "今天我跟蓝老师聊了什么")
    assert (
        result["status"] == "not_found"
        and result["evidence"] == []
        and result["claims"] == []
    )


@pytest.mark.parametrize("mode", ["timeout", "invalid", "insufficient"])
def test_model_failures_have_truthful_final_answers(env, mode):
    def model(p, c):
        if mode == "timeout":
            raise TimeoutError("synthetic timeout")
        if mode == "invalid":
            return {
                "claims": [
                    {
                        "kind": "summary",
                        "text": "伪造结论",
                        "citations": ["not-an-evidence"],
                    }
                ],
                "unknown": [],
            }
        return {"claims": [], "unknown": ["只有部分对话，无法确认完整内容。"]}

    env.service.model = model
    result = ask(env)
    assert result["evidence"] and result["claims"] == []
    assert result["model_status"] == (
        "insufficient" if mode == "insufficient" else "unavailable"
    )
    assert "无法生成" in result["answer"] and "伪造结论" not in result["answer"]
    assert result["metrics"]["model_calls"] == 1 and result["unknown"]


def test_source_offline_preserves_constraints_and_cached_summary(env):
    env.source.failure = "SOURCE_OFFLINE"
    result = ask(env, expand=True)
    assert result["source_status"] == "unavailable" and result["answer"] == SUMMARY
    assert any("SOURCE_OFFLINE" in line for line in result["coverage"])
    assert all(DAY <= r["sent_at"] < DAY + 86400 for r in result["evidence"])
    assert all(r["person_id"] in {"alice-person", "myself"} for r in result["evidence"])


def test_remote_both_sides_and_forged_source_scope(env):
    extra = [
        message(1300, "补充样品细节。"),
        message(1301, "我会核对。", "self-account"),
    ]
    env.source.records.extend(extra)
    result = ask(env, expand=True)
    assert {"rec:fixture-1300", "rec:fixture-1301"} <= {
        r["record_id"] for r in result["evidence"]
    }
    assert result["answer"] == SUMMARY
    assert all(
        "conversation_id" in params["scope"]
        and "sender_account_id" not in params["scope"]
        for route, body, params in env.source.calls
        if route == "/v1/messages"
    )
    original = env.source.request

    def forged(route, body=None, **params):
        if route == "/v1/messages":
            return {
                "items": [message(1400, "错误日期", sent=DAY - 1)],
                "has_more": False,
            }
        return original(route, body, **params)

    env.source.request = forged
    result = ask(env, expand=True)
    assert (
        result["source_status"] == "unavailable" and "错误日期" not in result["answer"]
    )
    assert not any(r["record_id"] == "rec:fixture-1400" for r in result["evidence"])


def test_context_opening_cannot_add_other_people_or_dates(env):
    result = ask(env)
    value = env.service.context("phone", result["query_id"], "chat:rec:fixture-1001")
    assert all(r["person_id"] in {"alice-person", "myself"} for r in value["evidence"])
    assert all(DAY <= r["sent_at"] < DAY + 86400 for r in value["evidence"])


def test_recording_speaker_is_not_proof_of_bilateral_conversation(env):
    result = ask(env, "昨天录音我跟蓝老师聊了什么")
    assert result["evidence"] == [] and result["model_status"] == "not_requested"
    assert any("不能证明" in v for v in result["coverage"])


@pytest.mark.parametrize(
    "now,zone,expected,length",
    [
        ("2027-01-01T00:01:00+08:00", "Asia/Singapore", "2026-12-31", 86400),
        ("2026-03-01T00:01:00+08:00", "Asia/Singapore", "2026-02-28", 86400),
        ("2026-03-09T00:01:00-04:00", "America/New_York", "2026-03-08", 23 * 3600),
        ("2026-11-02T00:01:00-05:00", "America/New_York", "2026-11-01", 25 * 3600),
        ("2026-10-14T01:00:00+08:00", "America/Los_Angeles", "2026-10-12", 86400),
    ],
)
def test_yesterday_calendar_day_and_timezone_boundaries(now, zone, expected, length):
    from zoneinfo import ZoneInfo

    period, _ = dates(
        "昨天我跟某人聊了什么", zone, int(datetime.fromisoformat(now).timestamp())
    )
    assert (
        datetime.fromtimestamp(period[0], ZoneInfo(zone)).date().isoformat() == expected
    )
    assert period[1] - period[0] + 1 == length


def test_week_date_range_invalid_dates_and_topic_only_plan(env):
    period, _ = dates("上周", "Asia/Singapore", NOW)
    assert datetime.fromtimestamp(period[0]).strftime("%Y-%m-%d") == "2026-10-05"
    period, _ = dates("2026-10-10到2026-10-12", "Asia/Singapore", NOW)
    assert period[1] - period[0] + 1 == 3 * 86400
    with pytest.raises(ValueError):
        dates("2026-02-30", "Asia/Singapore", NOW)
    with pytest.raises(ValueError):
        dates("昨天前天", "Asia/Singapore", NOW)
    p = understand(
        "昨天我跟蓝老师聊了什么", "", env.service.identities.people(), {}, NOW
    )
    assert p["terms"] == [] and p["scope"]["relationship"] == "conversation"


def test_real_topic_and_generic_fragments_cannot_create_hits(env):
    seen = []
    env.service.model = lambda p, c: seen.append(p) or summary_model(p, c)
    result = ask(env, "昨天我跟蓝老师关于样品聊了什么")
    assert result["terms"] == ["样品"] and result["answer"] == SUMMARY
    assert {r["conversation_id"] for r in result["evidence"]} == {"private-a"}
    empty = ask(env, "昨天我跟蓝老师关于海洋测绘聊了什么")
    assert (
        empty["evidence"] == []
        and empty["claims"] == []
        and "未找到" in empty["answer"]
    )
    unknown = ask(env, "昨天陌生老师说了什么")
    assert unknown["status"] == "needs_clarification" and unknown["evidence"] == []


def test_speaker_and_group_queries_retain_real_authorship(env):
    env.service.model = lambda p, c: {
        "claims": [
            {
                "kind": "quote",
                "text": p["records"][0]["quote"],
                "citations": [p["records"][0]["id"]],
            }
        ],
        "unknown": [],
    }
    speaker = ask(env, "昨天蓝老师说了什么")
    assert (
        speaker["scope"]["relationship"] == "speaker" and speaker["intent"] == "quote"
    )
    assert all(
        r["person_id"] == "alice-person"
        for r in speaker["evidence"]
        if r["role"] == "match"
    )
    group = ask(env, "昨天群聊讨论了什么")
    assert group["scope"]["relationship"] == "group"
    assert all(r["conversation_type"] == "group" for r in group["evidence"])
    by_id = {r["record_id"]: r for r in group["evidence"]}
    assert by_id["rec:fixture-1013"]["person_id"] == "bob-person"
    assert by_id["rec:fixture-1013"]["speaker"] == "乙同学"


def test_scope_conflict_does_not_silently_relax_filters(env):
    with pytest.raises(ValueError, match="不相交"):
        ask(env, sent_from=DAY + 86400, sent_to=DAY + 2 * 86400)
    with pytest.raises(ValueError, match="来源.*冲突"):
        ask(env, "昨天微信我跟蓝老师聊了什么", source="qq")
    assert env.source.calls == []


def test_correct_date_wrong_person_cannot_supply_target_answer(env):
    result = ask(env, "昨天我跟绿老师关于样品聊了什么")
    assert result["evidence"] == [] and result["model_status"] == "not_requested"
    assert result["claims"] == [] and SUMMARY not in result["answer"]


def test_source_offline_and_no_cached_interaction_is_not_success(env):
    env.source.failure = "SOURCE_OFFLINE"
    result = ask(env, "今天我跟蓝老师聊了什么", expand=True)
    assert (
        result["evidence"] == []
        and result["claims"] == []
        and "未找到" in result["answer"]
    )
    assert result["metrics"]["model_calls"] == 0


def test_calendar_date_and_clock_interval_are_constraints(env):
    result = ask(env, "10月13日09:00到10:00我跟蓝老师聊了什么")
    assert result["terms"] == [] and result["answer"] == SUMMARY
    assert result["scope"]["sent_from"] == DAY + 9 * 3600
    assert result["scope"]["sent_to"] == DAY + 10 * 3600 - 1
    afternoon = ask(env, "昨天下午我跟蓝老师聊了什么")
    assert (
        afternoon["evidence"] == []
        and afternoon["scope"]["sent_from"] == DAY + 12 * 3600
    )
    with pytest.raises(ValueError, match="明确起止"):
        ask(env, "昨天09:00我跟蓝老师聊了什么")
    with pytest.raises(ValueError, match="尚不支持"):
        ask(env, "去年10月13日我跟蓝老师聊了什么")


def test_literal_citation_does_not_reinterpret_quoted_person_or_date(env):
    quote = "蓝老师说了什么：昨天我跟蓝老师聊了什么？"
    r = message(1600, quote, "bob", conversation="private-b")
    env.cache.receive(env.source.dataset, [r])
    env.cache.process(100)
    result = ask(
        env,
        quote,
        keyword=quote,
        person_id="bob-person",
        sent_from=DAY,
        sent_to=DAY + 86400 - 1,
        summarize=False,
    )
    assert result["scope"]["person_id"] == "bob-person"
    assert result["intent"] == "quote" and result["scope"]["relationship"] == "speaker"
    assert (result["scope"]["sent_from"], result["scope"]["sent_to"]) == (
        DAY,
        DAY + 86400 - 1,
    )
    assert [v["record_id"] for v in result["evidence"] if v["role"] == "match"] == [
        r["record_id"]
    ]
    assert result["evidence"][0]["speaker"] == "乙同学"


def test_inference_in_primary_answer_is_explicitly_uncertain(env):
    env.service.model = lambda p, c: {
        "claims": [
            {
                "kind": "inference",
                "text": "可能还需核对检查安排。",
                "citations": [p["records"][0]["id"]],
            }
        ],
        "unknown": ["实际执行未知。"],
    }
    result = ask(env)
    assert result["answer"] == "推测：可能还需核对检查安排。"
    assert result["claims"][0]["citations"] and result["unknown"] == ["实际执行未知。"]


def test_other_confirmed_person_gets_own_bilateral_summary(env):
    rows = [
        message(1700, "服务器迁移考虑安排在周日。", "bob", conversation="migration"),
        message(
            1701,
            "我会准备回滚说明，是否迁移还未决定。",
            "self-account",
            conversation="migration",
        ),
    ]
    env.cache.receive(env.source.dataset, rows)
    env.cache.process(100)
    summary = "你们讨论了服务器迁移，考虑周日进行；本人准备回滚说明，迁移尚未决定。"

    def model(p, c):
        assert p["scope"]["person_id"] == "bob-person"
        selected = [r for r in p["records"] if r["conversation_id"] == "migration"]
        assert {r["person_id"] for r in selected} == {"bob-person", "myself"}
        return {
            "claims": [
                {
                    "kind": "summary",
                    "text": summary,
                    "citations": [r["id"] for r in selected],
                }
            ],
            "unknown": ["迁移是否执行尚未确认。"],
        }

    env.service.model = model
    result = ask(env, "昨天我跟绿老师聊了什么")
    assert result["answer"] == summary and result["model_status"] == "available"
    assert SUMMARY not in result["answer"] and result["claims"][0]["citations"]
