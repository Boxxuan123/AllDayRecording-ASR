"""Fictional positive/negative acceptance. No production state or external models."""

import json
import sqlite3
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from threading import Event, Thread
import pytest
from tests.chat_fixture import record, FixtureMac, NOW
from tests import test_v35_cross_day_person_memory as memory_fixture

from allday_asr.v3.adapters.sqlite import V3Database, SqliteUnitOfWork
from allday_asr.v3.adapters.sqlite.chat_cache import ChatCache
from allday_asr.v3.bootstrap.history_recall import (
    compose_history_recall as HistoryRecall,
)
from allday_asr.v3.domain.source_identity import identity_key
from allday_asr.v3.ports.chat_data import ChatDataError

UTTERANCE_IDS = memory_fixture.UTTERANCE_IDS


def unavailable(*args):
    raise TimeoutError("fictional model unavailable")


@pytest.fixture
def env(tmp_path):
    db = V3Database.open(tmp_path / "core.sqlite3")

    def factory():
        return SqliteUnitOfWork(db)

    with factory() as u:
        for pid, name, kind in [
            ("alice-person", "甲同学", "known"),
            ("bob-person", "乙同学", "known"),
            ("myself", "我", "self"),
        ]:
            u.people.create_person(
                pid, name, kind, datetime.now(timezone.utc).isoformat()
            )
    cache = ChatCache(tmp_path / "chat/cache.sqlite3")
    records = [
        record(1, "杭电：计划考虑计算机专业，因素是就业。", conversation="direct"),
        record(2, "杭电：我建议考虑自动化。", conversation="group"),
        record(3, "杭电：@alice 引用甲的话，决定报名。", sender="bob"),
        record(4, "杭电：还没决定，先看考试科目。", "wechat", "wx-direct"),
        record(5, "招物理老师：尚未回复。", conversation="direct"),
        record(6, "后来已经婉拒了。", conversation="direct"),
        record(7, "杭电：本人消息", sender="self-account"),
    ]
    for r in records:
        r.update(
            conversation_type="c2c" if "direct" in r["conversation_id"] else "group",
            is_from_self=r["sender_account_id"] == "self-account",
            self_identity_basis="verified_mac_direction",
        )
    records[2]["reply"] = {
        "record_id": records[0]["record_id"],
        "sender_account_id": "alice",
        "text": "杭电",
    }
    namespace = record(8, "杭电：同账号不同采集命名空间")
    namespace["source_account_id"] = "other-source"
    records.append(namespace)
    source = FixtureMac(records)
    cache.activate(source.dataset)
    cache.receive(source.dataset, records)
    cache.process()
    fixture = SimpleNamespace(
        factory=factory, session_ids={1: "session-a", 2: "session-b"}
    )
    for n in [1, 2]:
        memory_fixture.V35CrossDayPersonMemoryTests._seed_session(
            fixture,
            n,
            datetime.fromtimestamp(NOW + n, timezone.utc),
            "杭电：录音讨论招生方向",
        )
    with db.transaction() as c:
        c.execute(
            "UPDATE utterances SET evidence_json=? WHERE utterance_id=?",
            (
                json.dumps(
                    {
                        "person_annotation": {
                            "person_id": "alice-person",
                            "source": "human",
                        }
                    }
                ),
                UTTERANCE_IDS[1],
            ),
        )
    recall = HistoryRecall(db, tmp_path, source, unavailable)
    return recall, cache, db, records, source


def decision(service, rec, action="link", person="alice-person", operation=None):
    request = {
        "operation_id": operation or f"op-{time.monotonic_ns()}",
        "key": identity_key(rec),
        "person_id": person,
        "action": action,
        "basis": "隔离测试人工确认依据",
        "version": service.identities.version(),
    }
    return request, service.execute(
        "phone", {"action": "identity", "decision": request}
    )


def query(service, **extra):
    return service.execute(
        "phone",
        {
            "action": "query",
            "query_id": "query-" + str(time.monotonic_ns()),
            "question": "之前讨论过杭电哪些专业？当时提到的考虑因素是什么？",
            "keyword": "杭电",
            "sent_from": NOW - 10,
            "sent_to": NOW + 100,
            "summarize": False,
            "expand": False,
            **extra,
        },
    )


def dump(path):
    with sqlite3.connect(path) as c:
        return "\n".join(c.iterdump())


def test_stable_account_across_private_group_and_namespaces(env):
    service, cache, db, rows, source = env
    decision(service, rows[0])
    decision(service, rows[3])
    value = query(service, person_id="alice-person")
    matches = [e for e in value["evidence"] if e["role"] == "match"]
    assert {e["source"] for e in matches} == {"qq", "wechat", "recording"}
    assert {e["record_id"] for e in matches if e["source"] == "qq"} == {
        rows[0]["record_id"],
        rows[1]["record_id"],
    }
    assert rows[2]["record_id"] not in {e.get("record_id") for e in matches}
    assert rows[7]["record_id"] not in {e.get("record_id") for e in matches}
    assert UTTERANCE_IDS[2] not in {e.get("utterance_id") for e in matches}
    other = next(
        e for e in value["evidence"] if e.get("record_id") == rows[2]["record_id"]
    )
    assert other["role"] == "context" and other["person_id"] == ""
    identities = service.identities.catalog()["items"]
    assert (
        len(
            [
                i
                for i in identities
                if i["platform"] == "qq"
                and i["account"] == "alice"
                and i["source"] == "source-qq"
            ]
        )
        == 1
    )
    assert any(
        i["platform"] == "recording" and i["person_id"] == "alice-person"
        for i in identities
    )
    assert not any(
        i["platform"] == "recording" and i["source"] == "session-b" for i in identities
    )


def test_reject_unlink_restart_repeat_and_cache_invalidation(env):
    service, cache, db, rows, source = env
    req, res = decision(service, rows[0], operation="durable-operation")
    assert service.execute("phone", {"action": "identity", "decision": req}) == res
    value = query(service, person_id="alice-person")
    decision(service, rows[0], action="unlink")
    with pytest.raises(ValueError, match="过期|失效"):
        service.page("phone", value["query_id"])
    req, _ = decision(service, rows[1], action="reject")
    restarted = HistoryRecall(db, service.root, source, unavailable)
    cache.receive(source.dataset, rows)
    cache.process()
    assert not [
        e
        for e in query(restarted, person_id="alice-person")["evidence"]
        if e["source"] == "qq" and e["role"] == "match"
    ]
    item = next(
        i
        for i in restarted.identities.catalog()["items"]
        if i["key"] == identity_key(rows[0])
    )
    assert item["person_id"] == "" and item["decisions"][0]["action"] == "reject"
    with pytest.raises(ValueError):
        service.identities.decide({**req, "action": "link"}, "phone")
    decision(restarted, rows[0], person="bob-person")
    assert any(
        e["source"] == "qq"
        for e in query(restarted, person_id="bob-person")["evidence"]
    )


def test_mac_self_flag_and_manual_correction_win_without_voice_writes(env):
    service, cache, db, rows, source = env
    before = dump(db.path)
    value = query(service, person_id="myself")
    assert [e["record_id"] for e in value["evidence"] if e["role"] == "match"] == [
        rows[6]["record_id"]
    ]
    assert (
        "Mac"
        in next(e for e in value["evidence"] if e["role"] == "match")["identity_basis"]
    )
    decision(service, rows[6], person="bob-person")
    assert not [
        e
        for e in query(service, person_id="myself")["evidence"]
        if e["role"] == "match"
    ]
    assert dump(db.path) == before


def test_query_read_only_no_answer_and_model_failure_keeps_originals(env):
    service, cache, db, rows, source = env
    decision(service, rows[0])
    before = (dump(db.path), dump(cache.path))
    found = query(service, summarize=True)
    assert found["model_status"] == "timeout" and found["evidence"]
    none = query(service, keyword="虚构未出现的词", summarize=True)
    assert none["status"] == "not_found" and none["metrics"]["model_calls"] == 0
    assert (dump(db.path), dump(cache.path)) == before
    assert "不表示" in "".join(found["coverage"])


def test_evidence_conflicts_and_fake_quotes_rejected(env):
    service, cache, db, rows, source = env
    decision(service, rows[0])
    decision(service, rows[3])
    revisions = [dict(rows[0]), dict(rows[3])]
    for revised, statement in zip(
        revisions, ["杭电：已决定报计算机。", "杭电：没有决定报计算机。"], strict=True
    ):
        revised["record_revision"] += 1
        revised["text"] = statement
    cache.receive(source.dataset, revisions)
    cache.process()

    def model(payload, cancel):
        a = next(
            e for e in payload["records"] if e.get("record_id") == rows[0]["record_id"]
        )
        b = next(
            e for e in payload["records"] if e.get("record_id") == rows[3]["record_id"]
        )
        return {
            "claims": [
                {"kind": "quote", "text": a["quote"], "citations": [a["id"]]},
                {"kind": "quote", "text": b["quote"], "citations": [b["id"]]},
            ],
            "unknown": ["同一人物在两个来源的决定陈述矛盾，不能认定最终决定。"],
        }

    service.model = model
    value = query(service, summarize=True)
    assert value["model_status"] == "available" and len(value["claims"]) == 2
    assert value["metrics"]["model_calls"] == 1
    service.model = lambda p, c: {
        "claims": [
            {
                "kind": "quote",
                "text": "我已执行所有决定",
                "citations": [p["records"][0]["id"]],
            }
        ],
        "unknown": [],
    }
    bad = query(service, summarize=True)
    assert bad["model_status"] == "failed" and not bad["claims"] and bad["evidence"]


def test_remote_expansion_and_offline_preserve_readonly(env):
    service, cache, db, rows, source = env
    decision(service, rows[0])
    new = record(9, "远端唯一词：杭电新增专业", conversation="direct")
    source.records.append(new)
    before = (dump(db.path), dump(cache.path))
    found = query(service, keyword="远端唯一词", person_id="alice-person", expand=True)
    assert any(e.get("record_id") == new["record_id"] for e in found["evidence"])
    assert (dump(db.path), dump(cache.path)) == before
    source.failure = "MAC_OFFLINE"
    offline = query(
        service, keyword="远端唯一词", person_id="alice-person", expand=True
    )
    assert offline["source_status"] == "unavailable"
    context = service.context("phone", found["query_id"], "chat:" + new["record_id"])
    assert context["status"] == "unavailable" and context["evidence"]
    assert (dump(db.path), dump(cache.path)) == before


def test_bound_pagination_recording_revision_and_cancel(env):
    service, cache, db, rows, source = env
    more = [record(n, "分页唯一关键词", sent=NOW + n) for n in range(100, 230)]
    cache.receive(source.dataset, more)
    cache.process(200)
    value = query(service, keyword="分页唯一关键词", sent_to=NOW + 500)
    assert value["has_more"] and len(value["evidence"]) == 20
    assert value["metrics"]["hits"] == 100
    second = service.page("phone", value["query_id"], 1)
    assert not {e["id"] for e in value["evidence"]} & {
        e["id"] for e in second["evidence"]
    }
    with pytest.raises(ValueError):
        service.page("other-phone", value["query_id"], 0)
    rec = query(service, source="recording")
    with db.transaction() as c:
        c.execute(
            "UPDATE utterances SET revision=revision+1 WHERE utterance_id=?",
            (UTTERANCE_IDS[1],),
        )
    expired = service.page("phone", rec["query_id"])
    assert expired["status"] == "stale" and expired["model_status"] == "stale"
    assert not expired["claims"] and any(
        e.get("validity") == "stale" for e in expired["evidence"]
    )
    started = Event()

    def waiting(payload, cancel):
        started.set()
        assert cancel.wait(2)
        raise ChatDataError("CANCELLED", 409)

    service.model = waiting
    out = []
    thread = Thread(
        target=lambda: out.append(
            query(service, summarize=True, query_id="cancel-example")
        )
    )
    thread.start()
    assert started.wait(2)
    service.execute("phone", {"action": "cancel", "query_id": "cancel-example"})
    thread.join(3)
    assert not thread.is_alive() and out[0]["status"] == "cancelled"
    assert (
        service.execute("phone", {"action": "cancel", "query_id": "before-start"})[
            "status"
        ]
        == "cancelled"
    )
    assert query(service, query_id="before-start")["status"] == "cancelled"


def test_http_route_authenticates_before_recall_and_binds_request_body():
    from allday_asr.v3.interfaces.transfer.http_review_routes import (
        dispatch_review_post,
    )

    calls = []
    raw = b'{"action":"query","query_id":"test-http","question":"synthetic"}'

    class Handler:
        server = SimpleNamespace(
            v3_gateway=SimpleNamespace(
                recall=lambda key, payload: (
                    calls.append((key, payload)) or {"status": "not_found"}
                )
            )
        )

        def _read_body(self, **kwargs):
            return raw

        def _authenticate_device_request(self, binding):
            import hashlib

            assert binding.path == "/device/v3/recall" and binding.method == "POST"
            assert binding.body_sha256 == hashlib.sha256(raw).hexdigest()
            calls.append("authenticated")
            return SimpleNamespace(device_id="paired-phone")

        def _decode_json(self, body):
            return json.loads(body)

        def _send_json(self, status, response):
            calls.append(status)

    assert dispatch_review_post(Handler(), "/device/v3/recall")
    assert (
        calls[0] == "authenticated"
        and calls[1][0] == "paired-phone"
        and calls[-1] == 200
    )

    class Denied(Handler):
        def _authenticate_device_request(self, binding):
            raise PermissionError("unpaired")

    before = list(calls)
    with pytest.raises(PermissionError):
        dispatch_review_post(Denied(), "/device/v3/recall")
    assert calls == before


def test_timeout_partial_results_and_malformed_source_cannot_cross_identity(env):
    service, cache, db, rows, source = env
    decision(service, rows[0])
    original = service.sources.recordings
    service.sources.recordings = lambda *a: (_ for _ in ()).throw(TimeoutError())
    value = query(service, person_id="alice-person")
    assert value["status"] == "partial" and value["evidence"]
    service.sources.recordings = original
    request = source.request

    def forged(route, body=None, **kwargs):
        if route == "/v1/messages":
            return {"items": [rows[2]], "has_more": False}
        return request(route, body, **kwargs)

    source.request = forged
    value = query(
        service, keyword="exclusive-remote-word", person_id="alice-person", expand=True
    )
    assert not value["evidence"] and value["source_status"] == "unavailable"
    with pytest.raises(ValueError):
        query(service, sent_to=NOW + 367 * 86400)
    with pytest.raises(ValueError):
        query(service, person_id="unknown-recording-person")
    with pytest.raises(ValueError):
        decision(service, {**rows[0], "sender_account_id": "not-in-cache"})


def test_no_keyword_question_and_recording_unknown_have_positive_results(env):
    service, cache, db, rows, source = env
    value = query(service, keyword="")
    assert "杭电" in value["terms"] and value["metrics"]["hits"] > 0
    all_recordings = query(service, source="recording")
    assert len(all_recordings["evidence"]) == 2
    unknown = next(
        e for e in all_recordings["evidence"] if e["utterance_id"] == UTTERANCE_IDS[2]
    )
    assert unknown["person_id"] == "" and unknown["speaker"] == "未知说话人"


def test_context_cumulative_source_budget_and_rejection_survives_restart(env):
    service, cache, db, rows, source = env
    decision(service, rows[0], action="reject")
    value = query(service)
    for _ in range(7):
        context = service.context(
            "phone", value["query_id"], "chat:" + rows[0]["record_id"]
        )
    assert context["status"] == "unavailable" and "BUDGET" in context["notice"]
    assert service.page("phone", value["query_id"])["metrics"]["source_requests"] == 6
    restarted = HistoryRecall(db, service.root, source, unavailable)
    catalog = restarted.identities.catalog()
    identity = next(i for i in catalog["items"] if i["key"] == identity_key(rows[0]))
    assert identity["decisions"][0]["action"] == "reject"


def test_reject_current_link_and_unlink_mac_self_create_manual_tombstones(env):
    service, cache, db, rows, source = env
    decision(service, rows[0])
    decision(service, rows[0], action="reject")
    assert not [
        e
        for e in query(service, person_id="alice-person")["evidence"]
        if e["role"] == "match" and e["source"] == "qq"
    ]
    decision(service, rows[6], action="unlink", person="myself")
    cache.receive(source.dataset, rows)
    cache.process()
    assert not query(service, person_id="myself")["evidence"]


def test_existing_memory_projection_drops_old_person_label_on_unlink(env):
    from allday_asr.v3.adapters.sqlite.cross_day_memory_sources import (
        CrossDayMemorySources,
    )

    service, cache, db, rows, source = env
    decision(service, rows[0])
    sources = CrossDayMemorySources(db, lambda: SqliteUnitOfWork(db), cache.path)
    inventory = sources.inventory("2026-10-09", "2026-10-09", "Asia/Singapore")
    peer = next(
        p
        for p in inventory["contacts"]
        if p["account_id"] == "alice" and p["platform"] == "qq"
    )
    scope = sources.freeze("2026-10-09", "2026-10-09", "Asia/Singapore", [peer["id"]])
    assert any(
        p["person_id"] == "alice-person"
        for r in sources.load(scope)
        for p in r["contacts"]
    )
    decision(service, rows[0], action="unlink")
    refreshed = sources.load(scope)
    assert all(
        p["person_id"] is None and p["label"] != "甲同学"
        for r in refreshed
        for p in r["contacts"]
    )


def test_catalog_excludes_deleted_recordings_and_failed_start_is_retryable(
    env, monkeypatch
):
    service, cache, db, rows, source = env
    assert any(
        i["platform"] == "recording" for i in service.identities.catalog()["items"]
    )
    with db.transaction() as c:
        c.execute(
            "UPDATE recording_sessions SET tombstoned_at=? WHERE session_id='session-a'",
            ("2026-10-13T00:00:00Z",),
        )
    assert not any(
        i["platform"] == "recording" for i in service.identities.catalog()["items"]
    )
    version = service.identities.version
    monkeypatch.setattr(
        service.identities,
        "version",
        lambda: (_ for _ in ()).throw(sqlite3.OperationalError("temporarily locked")),
    )
    with pytest.raises(sqlite3.OperationalError):
        query(service, query_id="retry-start-1")
    assert not service.active
    monkeypatch.setattr(service.identities, "version", version)
    assert query(service, query_id="retry-start-1")["metrics"]["hits"] > 0
