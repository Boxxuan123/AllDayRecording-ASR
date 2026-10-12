"""Offline mixed-source integration: actual repositories, summary revision and sync feed."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import pytest
from tests.test_daily_event_layer import source as source
from tests.chat_fixture import record
from allday_asr.v3.adapters.sqlite.unified_review_sources import UnifiedReviewSources
from allday_asr.v3.adapters.sqlite.chat_cache import ChatCache
from allday_asr.v3.application.unified_review import UnifiedReviewService
from allday_asr.v3.domain.unified_review import validate_review


def prepare(source, tmp_path):
    seed, insights, factory = source
    insights.refresh_daily("2026-09-01")
    cache = ChatCache(tmp_path / "chat.sqlite3")
    start = int(datetime(2026, 9, 1, 0, tzinfo=timezone.utc).timestamp())
    r = record(501, "我准备明天交材料，今天还没有交", sent=start)
    r.update(conversation_type="c2c", is_from_self=True)
    cache.receive("dataset", [r])
    cache.process()
    other = record(502, "第三方下周开会，与我无关", sent=start + 1)
    other.update(
        conversation_type="group", is_from_self=False, conversation_id="unrelated"
    )
    cache.receive("dataset", [other])
    cache.process()
    sources = UnifiedReviewSources(seed.database, factory, cache.path)
    return sources, cache, seed, factory


def response(inputs):
    chat = next(e for e in inputs["evidence"] if e["source_type"] == "chat")
    recording = next(e for e in inputs["evidence"] if e["source_type"] == "recording")
    return {
        "headline": "当天回顾",
        "claims": [
            {
                "section": "followup",
                "text": "我准备明天交材料，今天还未报告提交。",
                "certainty": "fact",
                "perspective": "self",
                "event_ids": [],
                "evidence": [
                    {"evidence_id": chat["evidence_id"], "quote": chat["text"]}
                ],
            },
            {
                "section": "activity",
                "text": "录音中讨论了原文所述事项。",
                "certainty": "fact",
                "perspective": "observed",
                "event_ids": [],
                "evidence": [
                    {
                        "evidence_id": recording["evidence_id"],
                        "quote": recording["text"],
                    }
                ],
            },
        ],
        "limitations": ["未在资料中看到完成，不等于未完成。"],
    }


def test_mixed_day_repeat_failure_updates_only_summary(source, tmp_path):
    sources, cache, seed, factory = prepare(source, tmp_path)
    calls = []

    def model(inputs):
        calls.append(inputs)
        return response(inputs), {"model": "synthetic"}

    service = UnifiedReviewService(sources, factory, model, tmp_path / "receipts")

    def truth():
        with seed.database.read() as c:
            return [
                tuple(r) for r in c.execute("SELECT * FROM event_current_states")
            ], [tuple(r) for r in c.execute("SELECT * FROM reminder_schedules")]

    before = truth()
    review = service.generate("2026-09-01", "Asia/Singapore")
    assert review["status"] == "ready" and len(review["claims"]) == 2
    assert service.generate("2026-09-01", "Asia/Singapore")["reused"]
    assert len(calls) == 1 and truth() == before
    assert review["coverage"]["chat_records_selected"] == 1
    with factory().reading() as uow:
        summary = uow.insights.list_daily(2)[0]
    # Existing daily refresh cannot erase the unified result or create another revision.
    after = source[1].refresh_daily("2026-09-01")
    assert after["objective"]["unified_review"] == review
    assert after["revision"] == summary["revision"]
    # An older running writer may append a recording-only revision. Recover the
    # latest review for the same date without inference, then repair synchronization.
    with seed.database.transaction() as c:
        row = dict(
            c.execute(
                "SELECT * FROM daily_summary_revisions ORDER BY revision DESC LIMIT 1"
            ).fetchone()
        )
        row["revision"] += 1
        legacy = json.loads(row["objective_json"])
        legacy.pop("unified_review")
        row["objective_json"] = json.dumps(legacy)
        c.execute(
            "INSERT INTO daily_summary_revisions ("
            + ",".join(row)
            + ") VALUES ("
            + ",".join("?" for _ in row)
            + ")",
            tuple(row.values()),
        )
    assert service.get("2026-09-01", "Asia/Singapore")["status"] == "ready"
    service.refresh_saved()
    assert service.generate("2026-09-01", "Asia/Singapore")["reused"]
    assert len(calls) == 1 and truth() == before
    changed = record(
        501,
        "我答应明天提交，但今天还没有提交",
        sent=int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()),
    )
    changed.update(conversation_type="c2c", is_from_self=True, record_revision=2)
    cache.receive("dataset", [changed])
    cache.process()

    def fail(inputs):
        raise ValueError("synthetic provider failure")

    assert service.get("2026-09-01", "Asia/Singapore")["status"] == "input_changed"
    service.refresh_saved()
    with factory().reading() as uow:
        assert (
            uow.insights.list_daily(2)[0]["objective"]["unified_review"]["status"]
            == "input_changed"
        )
    assert len(calls) == 1 and truth() == before
    service.model = fail
    failed = service.generate("2026-09-01", "Asia/Singapore")
    assert failed["status"] == "failed" and failed["claims"] == review["claims"]
    assert failed["generated_at"] == review["generated_at"]
    assert service.get("2026-09-01", "Asia/Singapore")["status"] == "failed"
    assert truth() == before
    service.model = model
    updated = service.generate("2026-09-01", "Asia/Singapore")
    assert updated["status"] == "ready" and len(calls) == 2
    assert updated["input_sha256"] != review["input_sha256"]
    with seed.database.read() as c:
        assert (
            c.execute(
                "SELECT count(DISTINCT summary_id) FROM daily_summary_revisions"
            ).fetchone()[0]
            == 1
        )
        feed = json.loads(
            c.execute(
                "SELECT payload_json FROM change_events WHERE resource_type='daily_summary' ORDER BY sequence DESC LIMIT 1"
            ).fetchone()[0]
        )
        assert feed["objective"]["unified_review"]["status"] == "ready"


def test_no_invented_quote_identity_or_title_merge(source, tmp_path):
    sources, *_ = prepare(source, tmp_path)
    inputs = sources.load("2026-09-01", "Asia/Singapore")
    value = response(inputs)
    bad = deepcopy(value)
    bad["claims"][0]["evidence"][0]["quote"] = "不存在的原文"
    with pytest.raises(ValueError, match="quotation"):
        validate_review(bad, inputs)
    bad = deepcopy(value)
    bad["claims"][0]["evidence"] += bad["claims"][1]["evidence"]
    bad["claims"][0]["perspective"] = "observed"
    with pytest.raises(ValueError, match="association"):
        validate_review(bad, inputs)
    bad = deepcopy(value)
    bad["claims"][0]["event_ids"] = ["same-title"]
    with pytest.raises(ValueError, match="associated"):
        validate_review(bad, inputs)
    unknown = deepcopy(inputs)
    for e in unknown["evidence"]:
        e["is_from_self"] = None
    with pytest.raises(ValueError, match="self speech"):
        validate_review(value, unknown)


def test_occurrence_day_does_not_follow_mentioned_deadline(source, tmp_path):
    sources, *_ = prepare(source, tmp_path)
    inputs = sources.load("2026-09-01", "Asia/Singapore")
    assert any("明天" in e["text"] for e in inputs["evidence"])
    next_day = sources.load("2026-09-02", "Asia/Singapore")
    assert not any(e["source_type"] == "chat" for e in next_day["evidence"])


def test_individual_invalid_conclusion_does_not_discard_valid_facts(source, tmp_path):
    from allday_asr.v3.domain.unified_review import admit_review

    sources, *_ = prepare(source, tmp_path)
    inputs = sources.load("2026-09-01", "Asia/Singapore")
    value = response(inputs)
    bad = deepcopy(value["claims"][0])
    bad["evidence"][0]["quote"] = "无依据的新增原话"
    value["claims"].append(bad)
    grounded = admit_review(value, inputs)
    assert len(grounded["claims"]) == 2
    assert (
        grounded["validation_rejections"][0]["reason"]
        == "invented or unavailable quotation"
    )
    assert any("剔除" in line for line in grounded["limitations"])


def test_local_content_revision_is_idempotent_and_preserves_business_state(
    source, tmp_path
):
    sources, _, seed, factory = prepare(source, tmp_path)
    service = UnifiedReviewService(
        sources, factory, lambda i: (response(i), {}), tmp_path / "receipts"
    )
    previous = service.generate("2026-09-01", "Asia/Singapore")

    def forbidden(_):
        raise AssertionError("revision must never invoke inference")

    service.model = forbidden
    inputs = sources.load("2026-09-01", "Asia/Singapore")
    value = response(inputs)
    value["claims"][0]["section"] = "people"
    value["claims"][0]["text"] = "本人在聊天中表示准备明天交材料；没有报告已提交。"

    def business():
        with seed.database.read() as c:
            names = [
                r[0]
                for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
            ]
            return {
                name: [tuple(r) for r in c.execute('SELECT * FROM "' + name + '"')]
                for name in names
                if name.startswith(
                    ("event_", "reminder_", "session", "utterance", "chat_", "speaker")
                )
            }

    before = business()
    revised = service.revise(
        "2026-09-01",
        "Asia/Singapore",
        value,
        expected_input_sha256=previous["input_sha256"],
        reason="Synthetic evidence correction",
    )
    assert revised["claims"][0]["section"] == "people"
    assert revised["generated_at"] == previous["generated_at"]
    assert revised["revised_at"] and revised["coverage"] == previous["coverage"]
    with factory().reading() as uow:
        rev = uow.insights.list_daily(2)[0]["revision"]
    repeated = service.revise(
        "2026-09-01",
        "Asia/Singapore",
        value,
        expected_input_sha256=previous["input_sha256"],
        reason="Synthetic evidence correction",
    )
    assert repeated["reused"] and repeated["revised_at"] == revised["revised_at"]
    assert service.generate("2026-09-01", "Asia/Singapore")["reused"]
    with factory().reading() as uow:
        summaries = uow.insights.list_daily(2)
        assert len(summaries) == 1 and summaries[0]["revision"] == rev
    assert business() == before
    bad = deepcopy(value)
    bad["claims"][0]["evidence"][0]["quote"] = "没有出现在原文中的已完成行为"
    with pytest.raises(ValueError, match="quotation"):
        service.revise(
            "2026-09-01",
            "Asia/Singapore",
            bad,
            expected_input_sha256=previous["input_sha256"],
            reason="invalid",
        )
    with pytest.raises(ValueError, match="input changed"):
        service.revise(
            "2026-09-01",
            "Asia/Singapore",
            value,
            expected_input_sha256="superseded",
            reason="invalid",
        )
    assert service.get("2026-09-01", "Asia/Singapore") == revised
    assert business() == before


def test_mixed_self_claim_cannot_borrow_unknown_recording_identity(source, tmp_path):
    sources, *_ = prepare(source, tmp_path)
    inputs = sources.load("2026-09-01", "Asia/Singapore")
    value = response(inputs)
    chat = next(e for e in inputs["evidence"] if e["source_type"] == "chat")
    rec = next(e for e in inputs["evidence"] if e["source_type"] == "recording")
    value["claims"][0]["evidence"].append(
        {"evidence_id": rec["evidence_id"], "quote": rec["text"]}
    )
    # Even stable event association would not identify the recording speaker.
    chat["event_ids"] = rec["event_ids"] = ["stable-association"]
    with pytest.raises(ValueError, match="unidentified recording"):
        validate_review(value, inputs)
    value = response(inputs)
    value["claims"][1]["section"] = "people"
    with pytest.raises(ValueError, match="identified participant"):
        validate_review(value, inputs)


def test_other_person_task_is_not_a_personal_followup(source, tmp_path):
    sources, *_ = prepare(source, tmp_path)
    inputs = sources.load("2026-09-01", "Asia/Singapore")
    chat = next(e for e in inputs["evidence"] if e["source_type"] == "chat")
    chat["event_ids"] = ["other-task"]
    inputs["followups"] = [
        {"event_id": "other-task", "actor_is_from_self": False, "status": "confirmed"}
    ]
    value = response(inputs)
    value["claims"][0]["event_ids"] = ["other-task"]
    with pytest.raises(ValueError, match="reliable task ownership"):
        validate_review(value, inputs)
