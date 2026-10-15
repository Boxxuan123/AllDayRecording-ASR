"""Tri-state identity, fixed-ID refresh, immutable evidence and manual lifecycle tests."""

import copy
from threading import Event
import pytest
from tests.chat_fixture import record, NOW
from tests.test_chat_followups import env as env, candidate, selected, item
from allday_asr.v3.adapters.sqlite.chat_cache import ChatCache
from allday_asr.v3.application.chat_identity_refresh import IdentityRefresh
from allday_asr.v3.application.chat_followup_identity import rebuild_identity
from allday_asr.v3.domain.chat_identity import message_self, frozen_record
from allday_asr.v3.domain.chat_followups import validate_candidate
from allday_asr.v3.domain.chat_data import packed
from allday_asr.v3.ports.chat_data import ChatDataError, CONTRACT


def identity(r, flag):
    return {
        **r,
        "is_from_self": flag,
        "self_identity_basis": "qq.direction" if flag is not None else "unknown",
    }


def cache(tmp_path, records):
    c = ChatCache(tmp_path / "cache.sqlite3")
    c.activate("dataset")
    c.receive("dataset", records)
    c.process(5000)
    return c


class Source:
    def __init__(self, records):
        self.records = {r["record_id"]: r for r in records}
        self.calls = []
        self.failures = 0
        self.modify = lambda x: x

    def request(self, route, body=None):
        self.calls.append((route, copy.deepcopy(body)))
        if route == "/v1/contract":
            return {
                "api_contract_version": CONTRACT,
                "schema_version": 1,
                "self_identity": {"supported": True},
            }
        if route == "/v1/status":
            return {"dataset_id": "dataset"}
        assert route == "/v1/records" and len(body["items"]) <= 100
        if self.failures:
            self.failures -= 1
            raise ChatDataError("MAC_OFFLINE", 503)
        result = []
        for ref in body["items"]:
            r = self.records.get(ref["record_id"])
            result.append(
                {
                    **ref,
                    "status": "found" if r else "not_found",
                    "record": copy.deepcopy(r),
                }
            )
        return self.modify({"items": result})


@pytest.mark.parametrize("flag,expected", [(True, True), (False, False), (None, None)])
@pytest.mark.parametrize("platform", ["qq", "wechat"])
def test_flags_scoped_and_null_not_auto_inferred(tmp_path, flag, expected, platform):
    r = identity(record(1, "原话", platform=platform), flag)
    c = cache(tmp_path, [r])
    links = c.identity_links()["links"]
    assert message_self(r, links) is expected
    if flag is True:
        assert len(c.identity_links()["automatic"]) == 1
        other = {**r, "is_from_self": None}
        assert message_self(other, links) is None
        assert (
            message_self({**other, "source_account_id": "other-collection"}, links)
            is None
        )
        assert r["source_account_id"] != r["sender_account_id"]
    else:
        assert not c.identity_links()["automatic"]


def test_multiple_self_senders_manual_conflict_and_empty_sender(tmp_path):
    rows = [
        identity(record(1, "a", sender="one"), True),
        identity(record(2, "b", sender="two"), True),
        identity(record(3, "c", sender=None), True),
    ]
    c = cache(tmp_path, rows)
    c.save_manual_identity("qq", "source-qq", "one", "person-human")
    links = c.identity_links()
    assert len(links["automatic"]) == 2 and len(links["conflicts"]) == 1
    assert links["links"][packed(["qq", "source-qq", "one"])] == "person-human"
    assert message_self(rows[0], links["links"]) is True
    assert message_self(rows[2], links["links"]) is True
    assert rows[2]["sender_account_id"] is None
    with c.connect() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM chat_identity_manual_audit").fetchone()[0]
            == 1
        )


def test_body_revision_separate_from_live_identity(tmp_path):
    old = record(1, "不改原文")
    c = cache(tmp_path, [old])
    generation = c.get("generation")
    new = identity(old, True)
    c.receive("dataset", [new])
    assert c.record(old["record_id"])["is_from_self"] is True
    c.receive("dataset", [old])
    assert c.record(old["record_id"])["is_from_self"] is True
    assert c.get("generation") == generation
    with pytest.raises(ChatDataError, match="IMMUTABLE_REVISION_CHANGED"):
        c.receive("dataset", [{**new, "text": "改写"}])
    c.update_identity("dataset", identity(old, False))
    assert c.search({}, 1)["items"][0]["is_from_self"] is False
    assert frozen_record(c.record(old["record_id"])) == old


def test_fixed_ids_pause_resume_repeat_and_cursors(tmp_path):
    old = [record(i, "我负责处理", sender="s" + str(i % 2)) for i in range(205)]
    c = cache(tmp_path, old)
    source = Source([identity(r, i % 3 == 0) for i, r in enumerate(old)])
    c.put("fixed_cursor", {"boundary": "opaque", "position": 17})
    w = IdentityRefresh(c, source)
    job = w.create()
    first = w.run(job, max_batches=1)
    assert (
        first["success"] == 100
        and first["remaining"] == 105
        and first["state"] == "paused"
    )
    # New messages must not enter the captured refresh workset.
    c.receive("dataset", [record(1000, "不属于本次刷新")])
    result = IdentityRefresh(c, source).run(job)
    assert (
        result["success"] == 205 and result["remaining"] == 0 and result["failed"] == 0
    )
    assert c.get("fixed_cursor") == {"boundary": "opaque", "position": 17}
    assert w.run(job)["success"] == 205
    repeat = w.create(max_records=205)
    result = w.run(repeat)
    assert result["success"] + result["failed"] == 205
    assert all(
        len(body["items"]) <= 100
        for route, body in source.calls
        if route == "/v1/records"
    )


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "wrong_id",
        "wrong_revision",
        "changed_body",
        "no_flag",
        "invalid_flag",
        "unavailable_revision",
    ],
)
def test_per_record_failure_never_success(tmp_path, failure):
    old = record(1, "原文")
    c = cache(tmp_path, [old])
    s = Source([identity(old, True)])
    if failure == "missing":
        s.records = {}
    elif failure == "changed_body":
        s.records[old["record_id"]]["text"] = "变化"
    elif failure == "no_flag":
        s.records[old["record_id"]].pop("is_from_self")
    elif failure == "invalid_flag":
        s.records[old["record_id"]]["is_from_self"] = 0
    elif failure == "unavailable_revision":

        def modify(v):
            v["items"][0]["status"] = "revision_unavailable"
            return v

        s.modify = modify
    else:

        def modify(v):
            v["items"][0]["record"].update(
                {"record_id": "wrong"}
                if failure == "wrong_id"
                else {"record_revision": 2}
            )
            return v

        s.modify = modify
    w = IdentityRefresh(c, s)
    r = w.run(w.create())
    assert r["success"] == 0 and r["failed"] == 1
    assert frozen_record(c.record(old["record_id"])) == old


def test_retry_is_finite_and_can_stop(tmp_path):
    old = record(1, "x")
    c = cache(tmp_path, [old])
    s = Source([identity(old, True)])
    s.failures = 100
    stop = Event()
    w = IdentityRefresh(c, s, stop=stop)
    j = w.create()
    stop.set()
    assert w.run(j)["remaining"] == 1
    stop.clear()
    v = w.run(j)
    assert v["failed"] == 1 and v["success"] == 0
    assert sum(route == "/v1/records" for route, _ in s.calls) <= 3


@pytest.mark.parametrize("sender", ["different-native-id", None])
def test_self_commitment_needs_no_manual_selection(env, sender):
    r = identity(record(1, "我负责处理", sender=sender), True)
    c = candidate(r)
    v = validate_candidate(c, [r], "dataset", {}, NOW + 86400)
    assert v["category"] == "self" and v["actor_person_id"] == "self"
    result = env.service.apply(c, [r], "dataset", {})
    assert result["effect"] == "applied"
    assert env.service.apply(c, [r], "dataset", {})["effect"] == "duplicate"
    assert len(env.service.list()["items"]) == 1


def test_manual_ended_state_survives_identity_rebuild(env, tmp_path):
    old = record(1, "我负责处理")
    selected(env.service, candidate(old), [old], env.links)
    env.service.apply(candidate(old), [old], "dataset", env.links)
    first = item(env)
    env.service.act(first["source_key"], "complete", {}, first["revision"])
    ended = item(env)
    c = cache(tmp_path, [identity(old, False)])
    v = rebuild_identity(env.service, c)
    assert v["reinterpreted"] == 1
    latest = item(env)
    assert (
        latest["event_id"] == ended["event_id"]
        and latest["revision"] == ended["revision"]
    )
    assert (
        latest["status"] == ended["status"]
        and latest["human_override"] == ended["human_override"]
    )


def test_identity_rebuild_reuses_excluded_response_and_is_idempotent(env, tmp_path):
    r = record(1, "我负责处理", sender="new-id")
    assert env.service.apply(candidate(r), [r], "dataset", {})["effect"] == "excluded"
    c = cache(tmp_path, [identity(r, True)])
    assert rebuild_identity(env.service, c)["effects"] == {"applied": 1}
    row = item(env)
    assert row["chat"]["actor_is_from_self"] is True
    assert rebuild_identity(env.service, c)["effects"] == {"duplicate": 1}
    assert item(env)["revision"] == row["revision"]


def test_nested_reference_live_fields_and_original_revision(tmp_path):
    quote = record(2, "引用原文")
    old = {**record(1, "外层原文"), "quote": {"record": quote}}
    c = cache(tmp_path, [old])
    incoming = identity({**old, "quote": {"record": identity(quote, False)}}, True)
    c.update_identity("dataset", incoming)
    assert c.record(old["record_id"])["quote"]["record"]["is_from_self"] is False
    assert frozen_record(c.record(old["record_id"])) == frozen_record(old)


def test_context_and_saved_answer_identity_is_derived_only(tmp_path):
    old = record(1, "完整原文")
    c = cache(tmp_path, [old])
    c.put("context:dataset:test", {"items": [old]})
    c.job(
        "answer",
        "done",
        {"evidence": [old], "claims": [{"text": "原有回答", "kind": "inference"}]},
    )
    c.update_identity("dataset", identity(old, True))
    assert c.get("context:dataset:test")["items"][0]["is_from_self"] is True
    v = c.job("answer")["value"]
    assert (
        v["evidence"][0]["is_from_self"] is True
        and v["claims"][0]["text"] == "原有回答"
    )


def test_repeated_http_refresh_keeps_item_operation_revision(env, tmp_path):
    r = identity(record(1, "我负责处理", sender="new-id"), True)
    c = cache(tmp_path, [r])
    rebuild = env.service.apply(
        candidate(r), [r], "dataset", c.identity_links()["links"]
    )
    assert rebuild["effect"] == "applied"
    before = item(env)
    worker = IdentityRefresh(c, Source([r]))
    assert worker.run(worker.create())["changed"] == 0
    result = rebuild_identity(env.service, c)
    assert result["effects"] == {"duplicate": 1}
    assert item(env)["revision"] == before["revision"]


def test_explicit_false_overrides_manual_self_but_preserves_manual_link(tmp_path):
    r = identity(record(1, "非本人原话"), False)
    c = cache(tmp_path, [r])
    c.save_manual_identity("qq", "source-qq", "alice", "self")
    links = c.identity_links()
    assert links["manual"][packed(["qq", "source-qq", "alice"])] == "self"
    assert len(links["conflicts"]) == 1 and message_self(r, links["links"]) is False


@pytest.mark.parametrize(
    "action,changes",
    [
        ("complete", {}),
        ("cancel", {}),
        ("ignore", {}),
        ("edit", {"title": "用户改写标题"}),
        ("confirm", {"category": "self"}),
    ],
)
def test_live_card_speaker_metadata_preserves_user_decisions(
    env, tmp_path, action, changes
):
    old = identity(record(1, "我负责处理"), True)
    selected(env.service, candidate(old), [old], env.links)
    env.service.apply(candidate(old), [old], "dataset", env.links)
    first = item(env)
    env.service.act(first["source_key"], action, changes, first["revision"])
    before = item(env)
    c = cache(tmp_path, [identity(old, False)])
    env.service.identity_cache = c
    rebuild_identity(env.service, c)
    after = item(env)
    assert (
        after["revision"] == before["revision"]
        and after["status"] == before["status"]
        and after["ignored"] == before["ignored"]
    )
    assert after["payload"]["title"] == before["payload"]["title"]
    assert (
        after["chat"]["actor_is_from_self"] is False
        and after["chat"]["identity_conflict"] is True
    )


def test_changed_answer_evidence_marks_old_inference_stale(tmp_path):
    old = record(1, "原文")
    c = cache(tmp_path, [old])
    c.job(
        "old-answer",
        "done",
        {
            "generation": c.get("generation"),
            "evidence": [old],
            "claims": [{"text": "旧归纳"}],
        },
    )
    c.update_identity("dataset", identity(old, True))
    assert c.job("old-answer")["value"].get("self_identity_stale") is True


def test_crashed_refresh_cannot_reset_time_budget(tmp_path):
    import json
    import time

    old = record(1, "原文")
    c = cache(tmp_path, [old])
    s = Source([identity(old, True)])
    w = IdentityRefresh(c, s)
    j = w.create(seconds=1)
    with c.connect() as db:
        row = db.execute(
            "SELECT value FROM chat_identity_refresh_jobs WHERE id=?", (j,)
        ).fetchone()
        value = json.loads(row[0])
        value["active_since"] = time.time() - 5
        db.execute(
            "UPDATE chat_identity_refresh_jobs SET value=? WHERE id=?",
            (packed(value), j),
        )
    result = w.run(j)
    assert (
        result["error"] == "IDENTITY_BUDGET_EXHAUSTED"
        and result["remaining"] == 1
        and not s.calls
    )


def test_same_revision_receive_refreshes_context_and_invalidates_answer(tmp_path):
    old = record(1, "原文")
    c = cache(tmp_path, [old])
    c.put("context:dataset:record", {"items": [old], "has_before": True})
    c.job("old-answer", "done", {"evidence": [old], "claims": [{"text": "旧归纳"}]})
    generation = c.get("generation")
    c.receive("dataset", [identity(old, True)])
    assert c.get("generation") == generation
    assert c.get("context:dataset:record")["items"][0]["is_from_self"] is True
    assert c.get("context:dataset:record")["has_before"] is True
    assert c.job("old-answer")["value"]["self_identity_stale"] is True
    assert c.job("old-answer")["value"]["claims"] == [{"text": "旧归纳"}]


def test_stronger_real_style_evidence_replaces_partial_rule_audit(env, tmp_path):
    request = identity(record(201, "请你处理这件事", sender="bob"), False)
    brief = identity(record(202, "好", sender="alice"), True)
    promise = identity(record(203, "我负责处理这件事", sender="alice"), True)
    records = [request, brief, promise]
    c = cache(tmp_path, records)
    partial = candidate(
        request,
        actor_account_id="alice",
        evidence=[
            {"record_id": request["record_id"], "quote": request["text"]},
            {"record_id": brief["record_id"], "quote": brief["text"]},
        ],
    )
    assert (
        env.service.apply(partial, records, "dataset", c.identity_links()["links"])[
            "effect"
        ]
        == "excluded"
    )
    supported = {
        **partial,
        "evidence": partial["evidence"]
        + [
            {"record_id": promise["record_id"], "quote": promise["text"]},
        ],
    }
    assert (
        env.service.apply(supported, records, "dataset", c.identity_links()["links"])[
            "effect"
        ]
        == "applied"
    )
    before = item(env)
    result = rebuild_identity(env.service, c)
    after = item(env)
    assert result["effects"] == {"duplicate": 1}
    assert (
        after["event_id"] == before["event_id"]
        and after["revision"] == before["revision"]
    )
    assert after["status"] == "active" and after["chat"]["actor_is_from_self"] is True
    with env.factory().reading() as uow:
        audit = uow.followups.admission(before["source_key"], "dataset")
    assert audit["decision"] == "task" and audit["origin"] == "rule"
