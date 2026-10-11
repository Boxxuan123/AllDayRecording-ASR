"""Saved-candidate review is local, evidence bound and preserves human state."""

import pytest
from tests.test_chat_followups import env as env, jobs, wait, candidate
from tests.chat_fixture import record
from allday_asr.v3.ports.chat_data import ChatDataError


def review_job(env):
    r = record(910, "我来买一本资料")
    r.update(is_from_self=True, self_identity_basis="fixture.direction")
    calls = []
    c = candidate(r, uncertainty=["购买是否完成未知"])

    def model(*args):
        calls.append(1)
        return {"items": [c], "unknown": []}

    ctl = jobs(env, model)
    ctl.service.now = env.service.now
    ctl.chat.cache.receive("dataset", [r])
    ctl.start(records=[r], dataset="dataset", coverage=[{"complete": True}])
    wait(ctl)
    assert not ctl.service.list()["items"]
    review = ctl.review_candidates()["items"][0]
    assert not review["accepted"] and review["actor_is_from_self"] is True
    return ctl, review, calls


def accept(ctl, review, binding=None):
    return ctl.accept_candidate(
        review["job_id"],
        review["batch"],
        review["candidate"],
        review["binding"] if binding is None else binding,
    )


def test_visible_excluded_candidate_accepts_once_and_preserves_edits_completion(env):
    ctl, review, calls = review_job(env)
    bad = {**review["binding"], "evidence_digest": "wrong-evidence"}
    with pytest.raises(ChatDataError, match="RELEVANCE_BINDING_CHANGED"):
        accept(ctl, review, bad)
    assert not ctl.service.list()["items"]
    assert accept(ctl, review)["effect"] == "applied"
    item = ctl.service.list()["items"][0]
    assert item["category"] == "self" and item["human_override"]
    assert item["chat"]["uncertainty"] == ["购买是否完成未知"]
    assert item["chat"]["time"]["at"] is None
    assert ctl.review_candidates()["items"][0]["accepted"]
    assert accept(ctl, review)["effect"] == "already_accepted"
    assert len(ctl.service.list()["items"]) == 1
    assert ctl.service.list()["items"][0]["revision"] == item["revision"]
    ctl.service.act(
        item["source_key"], "edit", {"title": "用户修订标题"}, item["revision"]
    )
    edited = ctl.service.list()["items"][0]
    ctl.service.act(edited["source_key"], "complete", {}, edited["revision"])
    ended = ctl.service.list()["items"][0]
    accept(ctl, review)
    preserved = ctl.service.list()["items"][0]
    assert (
        preserved["status"] == "completed"
        and preserved["payload"]["title"] == "用户修订标题"
    )
    assert (
        preserved["revision"] == ended["revision"]
        and preserved["event_id"] == item["event_id"]
    )
    with env.factory().reading() as uow:
        admission = uow.followups.admission(item["source_key"], "dataset")
        assert (
            admission["origin"] == "human_selection" and admission["decision"] == "task"
        )
        assert uow.reminders.get_schedule(item["event_id"]) is None
    accepted_view = ctl.review_candidates()["items"][0]
    assert (
        accepted_view["category"] == "ended"
        and accepted_view["title"] == "用户修订标题"
    )
    assert calls == [1]
    ctl.close()


def test_changed_original_or_dataset_blocks_saved_candidate_review(env, monkeypatch):
    ctl, review, calls = review_job(env)
    original = ctl.chat.cache.record
    monkeypatch.setattr(
        ctl.chat.cache, "record", lambda key: {**original(key), "text": "已变更原文"}
    )
    with pytest.raises(ChatDataError, match="FOLLOWUP_EVIDENCE_CHANGED"):
        accept(ctl, review)
    assert not ctl.service.list()["items"]
    monkeypatch.setattr(ctl.chat.cache, "record", original)
    ctl.chat.cache.activate("other-dataset")
    with pytest.raises(ChatDataError, match="FOLLOWUP_SOURCE_CHANGED"):
        accept(ctl, review)
    assert not ctl.service.list()["items"] and calls == [1]
    ctl.close()
