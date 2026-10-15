"""Published cases are offline engineering regressions, never fresh blind tests."""

import copy
import json
import time
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace
import pytest
from tools.evaluate_isolated_agentic import seed, IsolatedSource, forbidden
from allday_asr.v3.adapters.sqlite.database import V3Database
from allday_asr.v3.bootstrap.history_recall import compose_history_recall
from tests.test_agentic_recall import response, action
from tests.test_history_recall import dump

FIXTURES = Path(__file__).parent / "fixtures/agentic_reliability"
CASES = json.loads((FIXTURES / "public_cases.json").read_text("utf-8"))["cases"]
PLANS = json.loads((FIXTURES / "published_first_plans.json").read_text("utf-8"))


def evaluate(case, root, monkeypatch, plan=None, setup=None, **options):
    if plan is not None:
        from allday_asr.v3.domain.recall_investigation import checked

        checked(copy.deepcopy(plan))
    seed(case, root)
    source = IsolatedSource(root)
    if setup:
        setup(source, root)
    inputs = []

    def fake(payload, cancel):
        inputs.append(copy.deepcopy(payload))
        if payload["round"] == 1:
            return copy.deepcopy(plan if plan is not None else PLANS[case["id"]])
        return response(
            "answer", unknown=["Offline protocol probe; real reasoning NOT_RUN."]
        )

    service = compose_history_recall(
        V3Database(root / "core.sqlite3"),
        root,
        source=source,
        model=forbidden,
        planner=forbidden,
        investigator=fake,
    )
    monkeypatch.setattr(
        "allday_asr.v3.adapters.codex.model_execution_runner.ModelExecutionRunner.run",
        forbidden,
    )
    now = int(datetime.fromisoformat(case["local_now"]).timestamp())
    monkeypatch.setattr(
        "allday_asr.v3.application.history_recall.time",
        SimpleNamespace(time=lambda: now, monotonic=time.monotonic),
    )
    before = (dump(root / "core.sqlite3"), dump(root / "chat/cache.sqlite3"))
    result = service.execute(
        "offline",
        {
            "action": "query",
            "query_id": "reliability-" + case["id"],
            "question": case["question"],
            "source": "qq",
            "timezone": case["timezone"],
            "expand": True,
            "summarize": True,
            **options,
        },
    )
    assert before == (dump(root / "core.sqlite3"), dump(root / "chat/cache.sqlite3"))
    return result, inputs, source


@pytest.mark.parametrize(
    "name,required",
    [
        ("A", {8101, 8102, 8103, 8104, 8105}),
        ("B", {8201, 8203, 8204, 8205, 8206}),
        ("C", {8301, 8302, 8303, 8304, 8305}),
    ],
)
def test_published_evidence_reaches_final_input(name, required, tmp_path, monkeypatch):
    case = next(c for c in CASES if c["id"] == name)
    result, inputs, source = evaluate(case, tmp_path / name, monkeypatch)
    ids = {r["record_id"] for r in inputs[-1]["records"]}
    assert {f"rec:fixture-{i}" for i in required} <= ids, (ids, result.get("coverage"))
    assert result["metrics"]["model_calls"] <= 2
    assert result["metrics"]["investigation_tools"] <= 8
    assert result["metrics"]["source_requests"] <= 12
    assert result["metrics"]["retrieval_requests"] <= 6
    assert result["claims"] == [] and not result["contact_candidates"]
    flow = result["evidence_flow"]
    assert flow["counts"]["model_selected"] >= len(required)
    assert all("quote" not in e and "text" not in e for e in flow["events"])


def unfamiliar():
    return {
        "id": "NEW",
        "question": "气象维护组2026年10月11日的“MX-73”后来怎样了？请核对后续说明。",
        "timezone": "Asia/Singapore",
        "local_now": "2026-10-15T12:00:00+08:00",
        "contacts": [
            {"id": "dispatch", "name": "气象维护组", "kind": "group"},
            {"id": "repair", "name": "器材校验室", "kind": "group"},
            {
                "id": "later",
                "name": "缪青",
                "kind": "c2c",
                "peer": "account-calibrator",
            },
            {
                "id": "namesake",
                "name": "缪青",
                "kind": "c2c",
                "peer": "account-unrelated",
            },
        ],
        "messages": [
            {
                "id": 91001,
                "conversation": "dispatch",
                "sender": "account-organizer",
                "at": "2026-10-11T09:00:00+08:00",
                "text": "MX-73 的湿度盒读数异常，我去校验室询问。",
            },
            {
                "id": 91002,
                "conversation": "repair",
                "sender": "account-organizer",
                "at": "2026-10-11T09:30:00+08:00",
                "text": "MX-73 请校验人员接手。",
            },
            {
                "id": 91003,
                "conversation": "repair",
                "sender": "account-calibrator",
                "at": "2026-10-11T09:31:00+08:00",
                "text": "MX-73 由我复检，后续会私下通知。",
            },
            {
                "id": 91004,
                "conversation": "later",
                "sender": "account-calibrator",
                "at": "2026-10-12T10:00:00+08:00",
                "text": "MX-73 换探头后仍偏高，不能交付。",
            },
            {
                "id": 91005,
                "conversation": "later",
                "sender": "self-account",
                "at": "2026-10-12T10:02:00+08:00",
                "text": "收到，等复核记录再决定。",
            },
            {
                "id": 91006,
                "conversation": "later",
                "sender": "account-calibrator",
                "at": "2026-10-12T10:03:00+08:00",
                "text": "MX-730 是另一批湿度盒，已经交付。",
            },
            {
                "id": 91007,
                "conversation": "namesake",
                "sender": "account-unrelated",
                "at": "2026-10-12T10:00:00+08:00",
                "text": "读数异常是另一场测验，不认识校验室的人。",
            },
            {
                "id": 91008,
                "conversation": "later",
                "sender": "account-calibrator",
                "at": "2026-10-15T10:00:00+08:00",
                "text": "MX-73 过了本次两天后续窗口才修好。",
            },
        ],
    }


def test_unfamiliar_identifier_followup_and_nearby_explanation(tmp_path, monkeypatch):
    result, inputs, source = evaluate(
        unfamiliar(),
        tmp_path / "new",
        monkeypatch,
        plan=response(actions=[action("find_conversations", terms=["组"])]),
    )
    ids = {r["record_id"] for r in inputs[-1]["records"]}
    assert {"rec:fixture-91004", "rec:fixture-91005"} <= ids
    assert not {"rec:fixture-91006", "rec:fixture-91007", "rec:fixture-91008"} & ids
    assert any(
        e["reason"] == "different_context_event_identifier"
        for e in result["evidence_flow"]["events"]
    )
    assert result["metrics"]["model_calls"] == 2
    assert not result["contact_candidates"]


def test_explicit_dates_block_later_messages(tmp_path, monkeypatch):
    case = unfamiliar()
    first = int(datetime.fromisoformat("2026-10-11T00:00:00+08:00").timestamp())
    result, inputs, source = evaluate(
        case,
        tmp_path / "hard-date",
        monkeypatch,
        plan=response(actions=[action("find_conversations", terms=["组"])]),
        sent_from=first,
        sent_to=first + 86399,
    )
    assert all(r["sent_at"] <= first + 86399 for p in inputs for r in p["records"])
    assert "followup_date_limited" in result["evidence_flow"]["gaps"]
    assert all(
        call["query"].get("scope", {}).get("sent_to", first) <= first + 86399
        for call in source.calls
    )


def test_source_offline_preserves_bounded_cache_and_reports_gap(tmp_path, monkeypatch):
    def offline(source, root):
        original = source.request

        def offline_reads(route, body=None, **params):
            if route in {"/v1/messages", "/v1/context"}:
                from allday_asr.v3.ports.chat_data import ChatDataError

                raise ChatDataError("SOURCE_OFFLINE")
            return original(route, body, **params)

        source.request = offline_reads

    result, inputs, _ = evaluate(
        unfamiliar(),
        tmp_path / "offline",
        monkeypatch,
        plan=response(actions=[action("find_conversations", terms=["组"])]),
        setup=offline,
    )
    assert any("SOURCE_OFFLINE" in s for s in result["coverage"])
    assert result["latest_status_confirmed"] is False
    assert result["metrics"]["model_calls"] == 2
    assert any(r["record_id"] == "rec:fixture-91004" for r in inputs[-1]["records"])


def test_late_capture_uses_message_time_and_pagination_is_reported(
    tmp_path, monkeypatch
):
    def late(source, root):
        row = copy.deepcopy(source.fixture.records[3])
        row.update(record_id="rec:fixture-late", captured_at="2026-10-15T02:00:00Z")
        source.fixture.records.append(row)
        original = source.request

        def paged(route, body=None, **params):
            result = original(route, body, **params)
            if route == "/v1/messages":
                result["has_more"] = True
            return result

        source.request = paged

    result, inputs, _ = evaluate(
        unfamiliar(),
        tmp_path / "late",
        monkeypatch,
        plan=response(actions=[action("find_conversations", terms=["组"])]),
        setup=late,
    )
    assert any(r["record_id"] == "rec:fixture-late" for r in inputs[-1]["records"])
    assert any("后续页" in s for s in result["coverage"])
    assert not result["latest_status_confirmed"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_account_id", "not-authorized"),
        ("platform", "wechat"),
        ("conversation_id", "another-room"),
        ("sent_at", 1),
    ],
)
def test_invalid_remote_scope_never_reaches_model(field, value, tmp_path, monkeypatch):
    def tamper(source, root):
        original = source.request

        def wrong(route, body=None, **params):
            result = original(route, body, **params)
            if route == "/v1/messages" and result["items"]:
                row = copy.deepcopy(result["items"][0])
                row["record_id"] = "rec:invalid-scope"
                row[field] = value
                result["items"].append(row)
            return result

        source.request = wrong

    result, inputs, _ = evaluate(
        unfamiliar(),
        tmp_path / "bad",
        monkeypatch,
        plan=response(actions=[action("find_conversations", terms=["组"])]),
        setup=tamper,
    )
    assert all(
        r["record_id"] != "rec:invalid-scope" for p in inputs for r in p["records"]
    )
    assert result["evidence_flow"]["counts"]["hard_rejected"] > 0


def test_withdrawn_cached_record_cannot_bridge_or_enter_model(tmp_path, monkeypatch):
    import sqlite3

    def withdrawn(source, root):
        with sqlite3.connect(root / "chat/cache.sqlite3") as conn:
            conn.execute("UPDATE records SET deleted=1 WHERE id='rec:fixture-91004'")

    result, inputs, _ = evaluate(
        unfamiliar(),
        tmp_path / "withdrawn",
        monkeypatch,
        plan=response(actions=[action("find_conversations", terms=["组"])]),
        setup=withdrawn,
    )
    assert all(
        r["record_id"] != "rec:fixture-91004" for p in inputs for r in p["records"]
    )
    assert any(
        "changed" in e["reason"] or "integrity" in e["reason"]
        for e in result["evidence_flow"]["events"]
        if e["stage"] == "hard_rejected"
    )


def test_third_person_mentions_are_not_identity_links(tmp_path, monkeypatch):
    case = copy.deepcopy(CASES[0])
    case["messages"] = [m for m in case["messages"] if m["id"] == 8106]
    result, inputs, _ = evaluate(
        case,
        tmp_path / "third",
        monkeypatch,
        plan=response(actions=[action("find_conversations", terms=["组"])]),
    )
    assert not result["contact_candidates"] and not result["claims"]
    assert result["scope"]["person_id"] == ""
    assert len(inputs) == 2
    assert any(r["record_id"] == "rec:fixture-8106" for r in inputs[0]["records"])


def test_anchor_syntax_is_not_specific_to_published_domains():
    from allday_asr.v3.domain.recall_event_anchor import event_anchors, literal_anchor

    assert event_anchors("图书馆‘修复登记册’以及 LP-984 的后续") == [
        "修复登记册",
        "LP-984",
    ]
    assert literal_anchor("LP-9840 属于别的修复登记", ["LP-984"]) is None
    assert literal_anchor("LP-984: shelf repaired", ["LP-984"]) == "LP-984"


def test_followup_available_only_remotely_reaches_final_input(tmp_path, monkeypatch):
    import sqlite3

    def remote_only(source, root):
        with sqlite3.connect(root / "chat/cache.sqlite3") as conn:
            conn.execute(
                "DELETE FROM records WHERE json_extract(body,'$.conversation_id')='later'"
            )

    result, inputs, source = evaluate(
        unfamiliar(),
        tmp_path / "remote-only",
        monkeypatch,
        plan=response(actions=[action("find_conversations", terms=["组"])]),
        setup=remote_only,
    )
    final = {r["record_id"]: r for r in inputs[-1]["records"]}
    assert {"rec:fixture-91004", "rec:fixture-91005"} <= final.keys()
    assert final["rec:fixture-91004"]["origin"] == "mac"
    assert result["metrics"]["source_requests"] <= 12
    assert result["metrics"]["investigation_tools"] <= 8


def test_directory_offline_stops_before_model_and_preserves_uncertainty(
    tmp_path, monkeypatch
):
    def offline(source, root):
        source.fixture.failure = "SOURCE_OFFLINE"

    result, inputs, _ = evaluate(
        unfamiliar(),
        tmp_path / "directory-offline",
        monkeypatch,
        plan=response(actions=[action("find_conversations", terms=["组"])]),
        setup=offline,
    )
    assert not inputs
    assert result["status"] == "needs_clarification"
    assert any("离线" in s for s in result["coverage"])
    assert not result["claims"]


def test_evidence_flow_counts_origin_and_deduplicates_at_each_stage():
    from allday_asr.v3.application.recall_evidence_flow import EvidenceFlow

    result = {}
    flow = EvidenceFlow(result)
    raw = {
        "record_id": "fictional",
        "record_revision": 1,
        "platform": "qq",
        "source_account_id": "owner",
    }
    cached = {
        "record_id": "fictional",
        "revision": 1,
        "source": "qq",
        "source_account_id": "owner",
        "dataset": "fixture",
    }
    flow.received([raw], "remote", "messages")
    flow.received([cached], "cache", "search")
    flow.record("candidate", cached, "accepted")
    flow.selected([cached], 2)
    counts = result["evidence_flow"]["counts"]
    assert counts["source_returned"] == counts["application_received"] == 1
    assert counts["cache_returned"] == counts["remote_returned"] == 1
    assert result["evidence_flow"]["observations"]["source_returned"] == 2
    assert result["evidence_flow"]["final_input_count"] == 1
    assert result["evidence_flow"]["model_rounds"]["2"] == ["fictional"]


def test_identifier_beside_chinese_is_literal_not_prefix():
    from allday_asr.v3.domain.recall_event_anchor import event_anchors, literal_anchor

    assert event_anchors("请查LP-984修复进展") == ["LP-984"]
    assert literal_anchor("LP-984已复核", ["LP-984"]) == "LP-984"
    assert literal_anchor("LP-9840已复核", ["LP-984"]) is None


@pytest.mark.parametrize("relation", ["reply_record_id", "quote_record_id"])
def test_explicit_context_relation_kept_without_initial_keywords_but_not_outside_scope(
    relation,
):
    from tests.test_agentic_review_fixes import remote_tools, raw, SCOPE

    tools, _ = remote_tools([])
    target = tools.service.sources.evidence(raw(text="LP-984: check"), "cache")
    linked = {
        **target,
        "id": "chat:reply",
        "record_id": "reply",
        "quote": "The receipt remains unverified",
        "sent_at": 1950,
        relation: target["record_id"],
    }
    assert tools.context_rows([linked], target, SCOPE, ["LP-984"]) == [
        {
            **linked,
            "retrieval_basis": "reply_reference",
            "relation_ids": [target["id"]],
            "relevance_confirmed": False,
        }
    ]
    forbidden = {**linked, "conversation_id": "another-room"}
    assert not tools.context_rows([forbidden], target, SCOPE, ["LP-984"])
    forbidden = {**linked, "sent_at": 2001}
    assert not tools.context_rows([forbidden], target, SCOPE, ["LP-984"])


def test_unlinked_distant_explanation_not_accepted_as_event_context():
    from tests.test_agentic_review_fixes import remote_tools, raw

    tools, _ = remote_tools([])
    target = tools.service.sources.evidence(raw(text="LP-984: check"), "cache")
    unrelated = {
        **target,
        "id": "chat:unrelated",
        "record_id": "unrelated",
        "quote": "An unrelated library receipt",
        "sent_at": 5000,
    }
    scope = {"sent_from": 0, "sent_to": 6000}
    assert not tools.context_rows([unrelated], target, scope, ["LP-984"])
    assert (
        tools.result["evidence_flow"]["events"][-1]["reason"]
        == "no_bounded_context_relation"
    )
