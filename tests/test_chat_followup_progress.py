"""Actual saved-response admission, then cross-batch progress; zero model network."""

import json
from datetime import datetime, timezone
from tests.test_chat_followups import env as env, wait, candidate
from tests.test_chat_followup_review import review_job, accept
from tests.chat_fixture import record
from allday_asr.v3.domain.chat_followups import validate_candidate


def test_accept_then_later_batch_completion_visible_and_decidable(env):
    ctl, review, calls = review_job(env, "我负责提交资料")
    accept(ctl, review)
    original = ctl.service.list()["items"][0]
    ctl.service.act(
        original["source_key"], "edit", {"title": "我确认的标题"}, original["revision"]
    )
    old = ctl.chat.cache.record(original["chat"]["anchor_id"])
    new = record(911, "资料已经发给你了", sent=old["sent_at"] + 100)
    new.update(is_from_self=True, self_identity_basis="fixture.direction")
    ctl.chat.cache.receive("dataset", [new])
    proposal = candidate(
        old,
        action="complete",
        target_event_id=original["event_id"],
        evidence=[
            {"record_id": old["record_id"], "quote": old["text"]},
            {"record_id": new["record_id"], "quote": new["text"]},
        ],
    )

    def replay(payload, *args):
        assert old["record_id"] in {r["record_id"] for r in payload["records"]}
        return {"items": [proposal], "unknown": []}

    ctl.model = replay
    job = ctl.start(records=[new], dataset="dataset", coverage=[{"complete": True}])
    wait(ctl)
    saved = ctl.service.job(job["id"])
    assert saved["failed_candidates"] == 0 and saved["pending_changes"] == 1
    current = ctl.service.list()["items"][0]
    assert (
        current["status"] == "active" and current["payload"]["title"] == "我确认的标题"
    )
    assert current["conflict"]["action"] == "complete"
    changes = [r for r in ctl.review_candidates()["items"] if r.get("change_pending")]
    assert len(changes) == 1 and not changes[0]["accepted"]
    ctl.service.act(current["source_key"], "accept_change", {}, current["revision"])
    ended = ctl.service.list()["items"][0]
    assert (
        ended["status"] == "completed"
        and ended["event_id"] == original["event_id"]
        and ended["payload"]["title"] == "我确认的标题"
    )
    assert (
        ctl.service.apply(proposal, [old, new], "dataset", ctl.links())["effect"]
        == "duplicate"
    )
    assert len(ctl.service.list()["items"]) == 1
    ctl.close()


def test_relative_change_uses_expression_record_not_identity_anchor(env):
    old = record(
        920,
        "我负责提交资料",
        sent=int(datetime(2026, 10, 9, tzinfo=timezone.utc).timestamp()),
    )
    new = record(
        921,
        "改到明天提交",
        sent=int(datetime(2026, 10, 11, tzinfo=timezone.utc).timestamp()),
    )
    c = candidate(
        old,
        action="update",
        target_event_id="test-event",
        time_expression="明天",
        due_date="2026-10-12",
        evidence=[
            {"record_id": old["record_id"], "quote": old["text"]},
            {"record_id": new["record_id"], "quote": new["text"]},
        ],
    )
    v = validate_candidate(
        c, [old, new], "dataset", env.links, 1791707260, "Asia/Shanghai"
    )
    assert (
        v["time"]["date"] == "2026-10-12" and v["time"]["record_id"] == new["record_id"]
    )
    assert v["anchor_id"] == old["record_id"]
    assert (
        validate_candidate(c, [old, new], "dataset", env.links, 1791707260)["time"][
            "date"
        ]
        is None
    )


def test_old_job_remains_visible_and_invalid_candidate_isolated(env, monkeypatch):
    ctl, review, _ = review_job(env)
    original = ctl.service.jobs()[0]
    path = ctl.root / (original["id"] + "-0.response.json")
    value = json.loads(path.read_text(encoding="utf-8"))
    bad = {**value["response"]["items"][0], "anchor_id": "missing"}
    value["response"]["items"].insert(0, bad)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    ctl.model = lambda *a: {"items": [], "unknown": []}
    ctl.start(records=[], dataset="dataset", coverage=[{"complete": True}])
    wait(ctl)
    rows = ctl.review_candidates()["items"]
    assert len(rows) == 2 and rows[0]["error"] and not rows[0]["can_accept"]
    assert rows[1]["can_accept"] and rows[1]["job_id"] == original["id"]
    ctl.close()
