"""Synthetic request modifiers exercise production retrieval and actual wire packing."""

from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from allday_asr.v3.domain.recall_query import dates
from tests.test_recall_subject_spans import parse
from tests.recall_scenario_driver import contact, message, run_scenario, selected_ids

NOW = 1792110900
ZONE = ZoneInfo("Asia/Singapore")


def stamp(day, hour=0, minute=0, second=0):
    return int(datetime(2025, 4, day, hour, minute, second, tzinfo=ZONE).timestamp())


@pytest.mark.parametrize("platform", ["微信", "QQ"])
def test_relative_conversation_clause_keeps_native_subject_and_topic(platform):
    p = parse(
        "昨天我和岚川在" + platform + "里那段约摄影练习的交流是什么过程？", ["岚川"]
    )
    assert p["person_mention"] == "岚川"
    assert not p["issue"]
    assert "摄影" in " ".join(p["terms"])


@pytest.mark.parametrize("name", ["住在那段街的", "岚川在里那段约摄影的"])
def test_full_known_native_label_is_not_trimmed_as_grammar(name):
    p = parse("昨天我和" + name + "交流了什么？", [name, "岚川"])
    assert p["person_mention"] == name


@pytest.mark.parametrize("subject", ["未确认的绰号", "岚川以及小禾"])
def test_unknown_or_multiple_names_are_not_replaced_by_known_prefix(subject):
    p = parse(
        "昨天我和" + subject + "在微信里那段约摄影的交流是什么过程？", ["岚川", "小禾"]
    )
    assert p["issue"]
    assert not p["scope"]["person_id"]


@pytest.mark.parametrize(
    "clock,seconds", [("18:20", 60), ("18:20:17", 1), ("23:59", 60)]
)
def test_single_clock_is_an_exact_message_point_not_a_day(clock, seconds):
    bounds, rest = dates(
        "2025年4月6日" + clock + "，那条消息表达什么？", "Asia/Singapore", NOW
    )
    parts = list(map(int, clock.split(":")))
    expected = stamp(6, *parts)
    assert bounds == (expected, expected + seconds - 1)
    assert clock not in rest


@pytest.mark.parametrize(
    "question",
    [
        "2025年4月6日25:20的消息",
        "2025年4月6日18:99的消息",
        "2025年4月6日18:20到的消息",
        "2025年4月6日18:20和19:30的消息",
        "2025年4月6日至2025年4月7日18:20的消息",
        "18:20的消息",
    ],
)
def test_incomplete_or_ambiguous_time_constraints_still_fail_closed(question):
    with pytest.raises(ValueError):
        dates(question, "Asia/Singapore", NOW)


def test_modifier_subject_reaches_original_native_private_source_and_wire(tmp_path):
    d = stamp(6)
    scenario = dict(
        question="2025年4月6日晚上，我和岚川在微信里那段约摄影练习的交流是什么过程？能确认已经参加了吗？",
        contacts=[
            contact("wechat", "private", "岚川", "peer"),
            contact("qq", "other-private", "岚川", "different-peer"),
        ],
        records=[
            message(
                901,
                "明天先看看天气，再确定是否出发。",
                "peer",
                "private",
                "wechat",
                hour=19,
                kind="c2c",
                day=d,
            ),
            message(
                902,
                "好，还没确认出发。",
                "self-account",
                "private",
                "wechat",
                hour=19,
                minute=1,
                kind="c2c",
                day=d,
            ),
            message(
                903,
                "另一平台同名者的无关活动。",
                "different-peer",
                "other-private",
                "qq",
                hour=19,
                kind="c2c",
                day=d,
            ),
        ],
        protocol_citation_probe=True,
    )
    run = run_scenario(tmp_path, scenario)
    assert run["internal"]["_plan"]["person_mention"] == "岚川"
    assert {"rec:fixture-901", "rec:fixture-902"} <= selected_ids(run)
    assert "rec:fixture-903" not in selected_ids(run)
    assert run["wire"] and run["result"]["model_status"] == "available"
    assert not run["internal"]["scope"]["person_id"]


def test_point_clock_group_reply_is_read_without_widening_minute(tmp_path):
    d = stamp(6)
    rows = [
        message(
            911,
            "先等进一步核实，不代表已经确认。",
            "peer",
            "garden",
            "wechat",
            hour=18,
            minute=20,
            day=d,
            label="云桐",
        ),
        message(
            912,
            "这个说法有依据吗？",
            "other",
            "garden",
            "wechat",
            hour=18,
            minute=20,
            day=d,
        ),
        message(
            913,
            "早一分钟不能当作目标时刻消息。",
            "peer",
            "garden",
            "wechat",
            hour=18,
            minute=19,
            day=d,
            label="云桐",
        ),
        message(
            914,
            "晚一分钟也不能偷偷扩大。",
            "peer",
            "garden",
            "wechat",
            hour=18,
            minute=21,
            day=d,
            label="云桐",
        ),
    ]
    rows[1]["sent_at"] += 5
    run = run_scenario(
        tmp_path,
        dict(
            question="2025年4月6日18:20，云桐在园艺交流群回复的那条消息表达了什么判断？",
            contacts=[contact("wechat", "garden", "园艺交流群", kind="group")],
            records=rows,
            protocol_citation_probe=True,
        ),
    )
    assert {"rec:fixture-911", "rec:fixture-912"} <= selected_ids(run)
    assert not {"rec:fixture-913", "rec:fixture-914"} & selected_ids(run)
    assert run["wire"]
    for r in run["internal"]["evidence"]:
        assert stamp(6, 18, 20) <= r["sent_at"] <= stamp(6, 18, 20, 59)


@pytest.mark.parametrize(
    "question",
    [
        "2025年4月6日下午安排18:20的会议了吗？",
        "2025年4月6日的计划提到18:20出发吗？",
        "2025年4月6日18:20:99那条消息",
    ],
)
def test_event_clock_is_not_silently_a_message_point(question):
    with pytest.raises(ValueError):
        dates(question, "Asia/Singapore", NOW)


@pytest.mark.parametrize("clock,expected_day", [("18:20", 15), ("01:20", 16)])
def test_night_point_stays_in_the_night_not_a_wrong_calendar_day(clock, expected_day):
    bounds, _ = dates("昨晚" + clock + "的消息", "Asia/Singapore", NOW)
    dt = datetime.fromtimestamp(bounds[0], ZONE)
    assert (dt.year, dt.month, dt.day) == (2026, 10, expected_day)
    assert bounds[1] - bounds[0] == 59 and bounds[1] <= NOW


@pytest.mark.parametrize(
    "question",
    [
        "2025年11月2日01:30的消息",
        "2025年3月9日02:30的消息",
    ],
)
def test_point_clock_does_not_guess_dst_fold_or_gap(question):
    with pytest.raises(ValueError, match="时区切换"):
        dates(question, "America/New_York", NOW)


def test_model_discovery_action_cannot_expand_an_explicit_message_minute(tmp_path):
    def decision(payload, value):
        if payload.get("round") == 1:
            objects = [
                o for o in payload.get("objects", []) if o.get("type") == "contact"
            ]
            assert objects
            value.update(
                mode="search",
                claims=[],
                unknown=[],
                actions=[
                    dict(
                        tool="search_messages",
                        purpose="Exercise the immutable message-time boundary",
                        targets=[objects[0]["ref"]],
                        terms=[],
                        window="discovery",
                        evidence_ids=[],
                    )
                ],
            )
        return value

    d = stamp(6)
    scenario = dict(
        question="2025年4月6日18:20园艺交流群的回复，能据此确认消息属实吗？",
        contacts=[contact("wechat", "garden", "园艺交流群", kind="group")],
        records=[
            message(
                931,
                "这一分钟请先核实。",
                "speaker",
                "garden",
                "wechat",
                hour=18,
                minute=20,
                day=d,
            ),
            message(
                932,
                "其他日期不能被宽查混入。",
                "speaker",
                "garden",
                "wechat",
                hour=18,
                minute=20,
                day=stamp(5),
            ),
        ],
        protocol_citation_probe=True,
    )
    run = run_scenario(tmp_path, scenario, decision=decision)
    assert any(w["task"] == "history-investigation-v1" for w in run["wire"])
    assert "rec:fixture-931" in selected_ids(run)
    assert "rec:fixture-932" not in selected_ids(run)
    reads = [c[2]["scope"] for c in run["source_queries"] if c[0] == "/v1/messages"]
    assert reads and all(
        stamp(6, 18, 20) <= s["sent_from"] <= s["sent_to"] <= stamp(6, 18, 20, 59)
        for s in reads
    )
