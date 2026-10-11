"""Synthetic collection/continuation regressions, never production or model calls."""

from types import SimpleNamespace
import pytest
from tests.chat_fixture import FixtureMac, record, NOW
from tests.test_chat_followups import env as env, jobs, wait
from allday_asr.v3.bootstrap.chat import compose_chat_queries
from allday_asr.v3.bootstrap.chat_followups import FollowupJobs
from allday_asr.v3.domain.chat_data import packed


def controller(env, fixture, monkeypatch):
    monkeypatch.setattr(
        "allday_asr.v3.bootstrap.chat_followup_collection.time.time",
        lambda: NOW + 20000,
    )
    chat = compose_chat_queries(
        env.root / "receipt", source=fixture, start_worker=False
    )
    core = SimpleNamespace(
        paths=SimpleNamespace(state_dir=env.root), database=env.db, reminders=None
    )
    calls = []

    def model(payload, *args):
        calls.append(payload)
        return {"items": [], "unknown": []}

    ctl = FollowupJobs(core, chat, model=model)
    ctl.configure(
        [
            {
                "platform": "qq",
                "source_account_id": "source-qq",
                "conversation_id": "group",
            }
        ],
        None,
    )
    return ctl, calls


def test_collect_has_no_thousand_record_or_hundred_page_cap(env, monkeypatch):
    fixture = FixtureMac(
        [record(i, "普通消息") for i in range(1, 10051)]
        + [record(11000, "末个来源", platform="wechat", conversation="wx")]
    )
    ctl, calls = controller(env, fixture, monkeypatch)
    ctl.configure(
        [
            {
                "platform": "qq",
                "source_account_id": "source-qq",
                "conversation_id": "group",
            },
            {
                "platform": "wechat",
                "source_account_id": "source-wechat",
                "conversation_id": "wx",
            },
        ],
        None,
    )
    value = {"coverage": []}
    ctl._collect(value)
    assert len(value["records"]) == 10051 and value["collection_complete"]
    assert all(c["complete"] for c in value["coverage"])
    assert [c["received"] for c in value["coverage"][1:]] == [10050, 1]
    assert len([c for c in fixture.calls if c[0] == "/v1/messages"]) == 102
    assert not calls and all(
        s["phase"] == "changes" and not s["enabled"] for s in ctl.chat.cache.scopes()
    )
    ctl.close()
    ctl.chat.close()


def test_legacy_partial_adds_remaining_batches_without_repeating_saved_response(
    env, monkeypatch
):
    fixture = FixtureMac([record(i, "普通消息") for i in range(1, 252)])
    ctl, calls = controller(env, fixture, monkeypatch)
    value = {"coverage": []}
    # Stop after the first atomic cache receipt, before a job checkpoint.
    original = ctl.chat.cache.receive

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt()

    monkeypatch.setattr(ctl.chat.cache, "receive", crash)
    with pytest.raises(KeyboardInterrupt):
        ctl._collect(value)
    scope = ctl.chat.cache.scopes()[0]
    assert scope["received"] == 100 and value["records"] == []
    monkeypatch.setattr(ctl.chat.cache, "receive", original)
    already = fixture.records[:50]
    key = "legacy-partial"
    saved = {
        "records": already,
        "batches": [already],
        "dataset": fixture.dataset,
        "coverage": value["coverage"],
        "calls": 1,
        "position": 1,
        "processing_watermark": 1,
        "applied": 0,
        "duplicate": 0,
        "failed_candidates": 0,
        "failed_batches": [],
        "created_at": NOW + 20000,
        "deadline_at": NOW + 20900,
        "error": None,
        "unknown": [],
        "timezone": None,
        "self_account_links": {},
    }
    # A legacy coverage cursor may lag; durable cache cursor is authoritative.
    saved["coverage"][-1]["next_cursor"] = None
    ctl.service.save_job(key, "partial", saved)
    (ctl.root / (key + "-0.response.json")).write_text(
        packed(
            {
                "source_records": already,
                "payload": {"records": already},
                "response": {"items": [], "unknown": []},
            }
        ),
        encoding="utf-8",
    )
    previous_queries = len([c for c in fixture.calls if c[0] == "/v1/messages"])
    ctl.start(resume_id=key)
    wait(ctl)
    result = ctl.service.job(key)
    assert result["state"] == "completed" and result["unique_messages"] == 251
    assert result["calls"] == 1 + len(calls) and result["processing_watermark"] == len(
        result["batches"]
    )
    seen = {r["record_id"] for call in calls for r in call["records"]}
    assert seen == {r["record_id"] for r in fixture.records[50:]}
    remaining_queries = [c for c in fixture.calls if c[0] == "/v1/messages"][
        previous_queries:
    ]
    assert (
        len(remaining_queries) == 2
        and fixture.read_cursor(
            remaining_queries[0][2]["cursor"], "query", remaining_queries[0][2]["scope"]
        )[0]
        == 100
    )
    assert all(c["complete"] for c in result["coverage"])
    ctl.close()
    ctl.chat.close()


def test_cancelled_collection_resumes_same_snapshot_and_window(env, monkeypatch):
    fixture = FixtureMac([record(i, "普通消息") for i in range(1, 252)])
    ctl, calls = controller(env, fixture, monkeypatch)
    original = ctl.chat.cache.receive

    def cancel(*args, **kwargs):
        original(*args, **kwargs)
        ctl._cancel.set()

    monkeypatch.setattr(ctl.chat.cache, "receive", cancel)
    job = ctl.start()
    wait(ctl)
    paused = ctl.service.job(job["id"])
    assert (
        paused["state"] == "paused" and paused["unique_messages"] == 100 and not calls
    )
    snapshot = paused["coverage"][-1]["snapshot_id"]
    window = paused["collection_window"]
    monkeypatch.setattr(ctl.chat.cache, "receive", original)
    ctl.start(resume_id=job["id"])
    wait(ctl)
    result = ctl.service.job(job["id"])
    assert result["state"] == "completed" and result["unique_messages"] == 251
    assert (
        result["coverage"][-1]["snapshot_id"] == snapshot
        and result["collection_window"] == window
    )
    assert len([c for c in fixture.calls if c[0] == "/v1/snapshots"]) == 1
    ctl.close()
    ctl.chat.close()


def test_stalled_pagination_is_error_not_false_complete(env, monkeypatch):
    fixture = FixtureMac([record(1, "普通消息")])
    ctl, calls = controller(env, fixture, monkeypatch)
    request = fixture.request
    pages = []

    def stalled(route, body=None, **query):
        if route != "/v1/messages":
            return request(route, body, **query)
        pages.append(1)
        return {
            "items": fixture.records,
            "has_more": True,
            "next_cursor": "same-cursor",
        }

    monkeypatch.setattr(fixture, "request", stalled)
    job = ctl.start()
    wait(ctl)
    result = ctl.service.job(job["id"])
    assert result["state"] == "failed" and result["error"] == "FOLLOWUP_CURSOR_STALLED"
    assert len(pages) == 2 and not calls and not result["coverage"][-1]["complete"]
    ctl.close()
    ctl.chat.close()


def test_more_than_thousand_messages_keeps_model_budget(env, monkeypatch):
    calls = []
    ctl = jobs(env, lambda *a: calls.append(1) or {"items": [], "unknown": []})
    records = [record(i, "普通消息") for i in range(1, 1202)]
    monkeypatch.setattr(
        ctl,
        "_batch",
        lambda v: [v["records"][i : i + 100] for i in range(0, len(v["records"]), 100)],
    )
    job = ctl.start(records=records, dataset="dataset", coverage=[{"complete": True}])
    wait(ctl)
    result = ctl.service.job(job["id"])
    assert (
        result["unique_messages"] == 1201
        and result["error"] == "FOLLOWUP_BUDGET_EXHAUSTED"
    )
    assert (
        result["state"] == "failed"
        and result["calls"] == 12
        and len(calls) == 12
        and result["position"] == 12
    )
    ctl.close()


def test_expired_model_budget_still_finishes_collection_without_new_model(
    env, monkeypatch
):
    fixture = FixtureMac([record(i, "普通消息") for i in range(1, 202)])
    ctl, calls = controller(env, fixture, monkeypatch)
    value = {"coverage": []}
    original = ctl.chat.cache.receive

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt()

    monkeypatch.setattr(ctl.chat.cache, "receive", crash)
    with pytest.raises(KeyboardInterrupt):
        ctl._collect(value)
    monkeypatch.setattr(ctl.chat.cache, "receive", original)
    existing = fixture.records[:100]
    saved = {
        "records": existing,
        "batches": [existing],
        "dataset": fixture.dataset,
        "coverage": value["coverage"],
        "calls": 1,
        "position": 1,
        "processing_watermark": 1,
        "applied": 0,
        "duplicate": 0,
        "failed_candidates": 0,
        "failed_batches": [],
        "created_at": NOW,
        "deadline_at": NOW + 1,
        "error": None,
        "unknown": [],
        "timezone": None,
        "self_account_links": {},
    }
    ctl.service.save_job("expired", "partial", saved)
    ctl.start(resume_id="expired")
    wait(ctl)
    result = ctl.service.job("expired")
    assert (
        result["unique_messages"] == 201
        and result["collection_complete"]
        and all(c["complete"] for c in result["coverage"])
    )
    assert (
        result["error"] == "FOLLOWUP_BUDGET_EXHAUSTED"
        and result["calls"] == 1
        and result["position"] == 1
        and not calls
    )
    assert result["deadline_at"] == NOW + 1
    ctl.close()
    ctl.chat.close()


def test_explicit_time_window_grant_retains_cumulative_call_cap(env, monkeypatch):
    calls = []

    def model(payload, cancel, batch_id, seconds):
        calls.append(batch_id)
        return {"items": [], "unknown": []}

    ctl = jobs(env, model)
    records = [record(i, "普通消息") for i in range(1, 8)]
    saved = {
        "records": records,
        "batches": [[r] for r in records],
        "dataset": "dataset",
        "coverage": [{"complete": True}],
        "calls": 5,
        "position": 5,
        "processing_watermark": 5,
        "applied": 0,
        "duplicate": 0,
        "failed_candidates": 0,
        "failed_batches": [],
        "created_at": NOW,
        "deadline_at": NOW + 1,
        "error": "FOLLOWUP_BUDGET_EXHAUSTED",
        "unknown": [],
        "timezone": None,
        "self_account_links": {},
    }
    ctl.service.save_job("grant", "failed", saved)
    ctl.start(resume_id="grant", renew_model_window=True, additional_calls=1)
    wait(ctl)
    result = ctl.service.job("grant")
    assert (
        result["calls"] == 6
        and result["position"] == 6
        and result["state"] == "failed"
        and result["error"] == "FOLLOWUP_BUDGET_EXHAUSTED"
    )
    assert len(calls) == 1 and calls[0] == "grant:window:1"
    assert result["model_window_history"] == [
        {"deadline_at": NOW + 1, "call_limit": 12, "calls_used": 5}
    ]
    assert result["model_call_limit"] == 6
    result["calls"] = 12
    ctl.service.save_job(
        "grant", "failed", {k: v for k, v in result.items() if k not in {"id", "state"}}
    )
    from allday_asr.v3.ports.chat_data import ChatDataError

    with pytest.raises(ChatDataError, match="FOLLOWUP_BUDGET_EXHAUSTED"):
        ctl.start(resume_id="grant", renew_model_window=True, additional_calls=12)
    assert len(calls) == 1
    ctl.close()
