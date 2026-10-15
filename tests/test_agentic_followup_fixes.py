"""Failure-driven regressions; no real-model calls or held-out stories."""

import copy
from types import SimpleNamespace
import pytest
from tests.test_agentic_recall import lab as lab_fixture, ask, response, action, claim
from tests.test_recall_query_loop import message, DAY
from allday_asr.v3.domain.recall_investigation import checked
from allday_asr.v3.adapters.codex.model_execution_runner import (
    _direct_call,
    _reported_tokens,
)


@pytest.fixture
def lab(tmp_path, monkeypatch):
    return lab_fixture.__wrapped__(tmp_path, monkeypatch)


def test_context_support_operands_are_normalized_without_mutating_input(lab):
    original = response(
        actions=[action("get_message_context", evidence=["chat:rec:fixture-5001"])]
    )
    normalized = checked(original)
    assert normalized["actions"][0]["targets"] == ["chat:rec:fixture-5001"]
    assert original["actions"][0]["targets"] == []
    lab.service.investigator = lambda p, c: (
        original
        if p["round"] == 1
        else response("answer", claims=[claim("来源陈述", ["chat:rec:fixture-5001"])])
    )
    result = ask(lab)
    assert result["model_status"] == "available"
    context = next(
        s for s in result["investigation_steps"] if s["tool"] == "get_message_context"
    )
    assert context["targets"] and context["hits"]


@pytest.mark.parametrize(
    "tool",
    [
        "get_message_context",
        "get_source_evidence",
        "get_related_conversations",
        "get_group_members",
        "search_messages",
        "find_contacts",
        "find_conversations",
    ],
)
def test_empty_operands_fail_explicitly(tool):
    with pytest.raises(ValueError):
        checked(response(actions=[action(tool)]))


def test_unknown_event_time_cannot_carry_quote_or_date():
    value = response("answer", claims=[claim("来源陈述", ["source"])])
    assert checked(value) == value
    for field in ("first", "last", "quote", "evidence_id"):
        bad = copy.deepcopy(value)
        bad["claims"][0]["event_time"][field] = "nonempty"
        with pytest.raises(ValueError, match="event time"):
            checked(bad)


def test_sdk_phases_distinguish_submit_from_result_wait(tmp_path):
    events = []

    def wait():
        assert events[-1] == "turn_wait"
        raise TimeoutError("synthetic transport wait")

    thread = SimpleNamespace(turn=lambda *a, **kw: SimpleNamespace(run=wait))
    client = SimpleNamespace(thread_start=lambda **kw: thread)
    with pytest.raises(TimeoutError):
        _direct_call(client, "{}", "test", {}, None, "low", tmp_path, events.append)
    assert events == ["thread_start", "turn_submit", "turn_wait"]


def test_nested_sdk_usage_counts_total_not_last_twice():
    assert (
        _reported_tokens(
            {
                "total": {"inputTokens": 17, "outputTokens": 3, "totalTokens": 20},
                "last": {"totalTokens": 7},
            }
        )
        == 20
    )


def test_four_actions_follow_second_speaker_to_next_day(lab):
    lab.contacts.append(
        {
            "platform": "qq",
            "source_account_id": "source-qq",
            "id": "bridge",
            "conversation_type": "group",
            "display_name": "协作讨论室",
        }
    )
    extra = [
        message(
            5010,
            "岬湾观测资料转交协作方核对。",
            "peer-r",
            conversation="bridge",
            kind="group",
            sent=DAY + 41000,
        ),
        message(
            5011,
            "岬湾观测已接收核对。",
            "peer-other",
            conversation="bridge",
            kind="group",
            sent=DAY + 42000,
        ),
        message(
            5012,
            "岬湾观测昨天未实施，仍待核对。",
            "peer-other",
            conversation="unrelated",
            sent=DAY + 86400 + 40000,
        ),
    ]
    lab.source.records.extend(extra)
    lab.cache.receive(lab.source.dataset, extra)
    lab.cache.process(100)

    def model(p, c):
        if p["round"] == 1:
            seed = "chat:rec:fixture-5001"
            return response(
                actions=[
                    action("get_related_conversations", [seed], window="followup"),
                    action("search_messages", ["$0"], ["岬湾观测"], "followup", [seed]),
                    action("get_related_conversations", ["$1"], window="followup"),
                    action("search_messages", ["$2"], ["岬湾观测"], "followup", [seed]),
                ]
            )
        ids = {r["id"] for r in p["records"]}
        assert {
            "chat:rec:fixture-5001",
            "chat:rec:fixture-5010",
            "chat:rec:fixture-5011",
            "chat:rec:fixture-5012",
        } <= ids
        return response(
            "answer",
            claims=[
                claim(
                    "后续发言称昨天未实施。",
                    ["chat:rec:fixture-5012"],
                    event={
                        "basis": "relative",
                        "first": "2026-10-13",
                        "last": "2026-10-13",
                        "quote": "昨天",
                        "evidence_id": "chat:rec:fixture-5012",
                    },
                )
            ],
        )

    lab.service.investigator = model
    result = ask(lab)
    assert result["model_status"] == "available", result
    assert result["metrics"]["model_calls"] == 2
    assert result["claims"][0]["event_time"]["first"] == "2026-10-13"
