"""Frozen fictional corpus: retrieval, selection and model protocol measured separately."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path
import pytest
from tests.test_history_recall import (
    env as recall_env,
    query,
    decision,
    dump,
    HistoryRecall,
    unavailable,
)
from tests.chat_fixture import record, NOW
from tests.test_cross_day_memory_quality import row
from allday_asr.v3.domain.memory_content import content, continuation_reason
from allday_asr.v3.domain.discussion_boundaries import split
from allday_asr.v3.domain.recall_language import plan, shared_anchor

CORPUS = Path(__file__).parent / "fixtures/recall_corrections/frozen.json"
FIXED = json.loads(CORPUS.read_text(encoding="utf-8"))


@pytest.fixture
def env(tmp_path):
    return recall_env.__wrapped__(tmp_path)


def save(cache, source, records):
    for r in records:
        r.update(conversation_type="c2c", is_from_self=False)
    source.records.extend(deepcopy(records))
    cache.receive(source.dataset, records)
    cache.process(200)


def echo(payload, cancel):
    r = payload["records"][-1]
    return {
        "claims": [{"kind": "quote", "text": r["quote"], "citations": [r["id"]]}],
        "unknown": ["仅固定测试适配器验证引用协议，不代表真实模型理解通过。"],
    }


def test_corpus_frozen_before_implementation():
    assert (
        hashlib.sha256(CORPUS.read_bytes()).hexdigest()
        == "87cc6150d2d95db6dc925a10d68114f90730d8400bd8aff258750a4a132b8762"
    )


@pytest.mark.parametrize("case", FIXED["semantics"], ids=lambda c: c["id"])
def test_no_semantic_fact_invention_in_excerpt_fallback(case):
    title, summary = content(row(case["text"]))
    assert "原文" in title and "不推断事实状态" in summary
    assert "「" + case["text"] + "」" in summary
    assert summary == "原文摘录（不推断事实状态）：「" + case["text"] + "」"


@pytest.mark.parametrize("case", FIXED["queries"], ids=lambda c: c["id"])
def test_natural_questions_retrieve_and_enter_model_without_keyword(env, case):
    service, cache, db, records, source = env
    target = record(200, case["evidence"], conversation="fixed")
    distractor = record(201, "今天是否需要买茶叶，我们再问一下。", conversation="noise")
    save(cache, source, [target, distractor])
    seen = []
    service.model = lambda p, c: seen.append(deepcopy(p)) or echo(p, c)
    value = query(
        service,
        question=case["question"],
        keyword="",
        source="qq",
        sent_to=NOW + 500,
        summarize=True,
    )
    assert "chat:" + target["record_id"] in value["metrics"]["model_input_ids"]
    assert any(
        e["record_id"] == target["record_id"] and e["role"] == "match"
        for e in value["evidence"]
    )
    assert value["intent"] == case["intent"] and value["metrics"]["model_calls"] == 1
    assert all(
        any(
            claim["text"] in r["quote"]
            for r in seen[0]["records"]
            if "chat:" + r["record_id"] in claim["citations"]
        )
        for claim in value["claims"]
    )
    filtered = query(
        service,
        question=case["question"],
        keyword="",
        source="wechat",
        sent_to=NOW + 500,
    )
    assert not any(
        e.get("record_id") == target["record_id"] for e in filtered["evidence"]
    )


@pytest.mark.parametrize("case", FIXED["boundary_pairs"])
def test_generic_context_boundaries_have_positive_and_negative_pairs(case):
    a, b = record(1, case["a"]), record(2, case["b"])
    groups = split([a, b])
    # Frozen labels describe related wording, not verified discussion identity.
    assert bool(shared_anchor(case["a"], case["b"])) == case["linked"]
    assert len(groups) == 2
    x, y = row(case["a"]), row(case["b"])
    y["occurred_at"] = "2026-10-07T08:10:00+08:00"
    assert continuation_reason(x, y) is None
    if case["linked"]:
        b["reply"] = {"record_id": a["record_id"]}
        assert len(split([a, b])) == 1


def test_reply_edges_group_unseen_objects_without_vocabulary():
    a = record(1, "码头试剂已编号，稍后送检。")
    b = record(2, "收到，先核对授权文件。")
    b["reply"] = {"record_id": a["record_id"]}
    assert len(split([a, b])) == 1
    b["reply"] = None
    assert len(split([a, b])) == 2


def test_latest_cancel_of_31_hits_enters_twenty_evidence_model_budget(env):
    service, cache, db, _, source = env
    rows = [
        record(200 + i, "星港展览：筹备记录 " + str(i), conversation="exhibit")
        for i in range(31)
    ]
    rows[0]["text"] = "星港展览：因为场地合适，计划周六举办。"
    rows[30]["text"] = "星港展览已经取消。"
    save(cache, source, rows)
    seen = []
    service.model = lambda p, c: seen.append(p) or echo(p, c)
    value = query(
        service,
        question="星港展览最终决定是什么",
        keyword="星港展览",
        source="qq",
        sent_to=NOW + 500,
        summarize=True,
    )
    assert value["metrics"]["hits"] == 31
    assert len(seen[0]["records"]) == 20
    assert {rows[0]["record_id"], rows[-1]["record_id"]} <= {
        r.get("record_id") for r in seen[0]["records"]
    }
    assert [r["sent_at"] for r in seen[0]["records"]] == sorted(
        r["sent_at"] for r in seen[0]["records"]
    )
    assert value["claims"][0]["text"] == "星港展览已经取消。"
    assert value["latest_status_confirmed"] is False


def test_five_old_hits_do_not_suppress_source_followup(env):
    service, cache, db, base, source = env
    decision(service, base[0])
    rows = [
        record(300 + i, "星港展览准备记录 " + str(i), conversation="exhibit")
        for i in range(5)
    ]
    save(cache, source, rows)
    newer = record(320, "已经取消，请勿再订车。", conversation="exhibit", sender="bob")
    source.records.append(newer)
    before = (dump(db.path), dump(cache.path))
    service.model = echo
    value = query(
        service,
        question="星港展览后来怎么样",
        keyword="星港展览",
        person_id="alice-person",
        source="qq",
        sent_to=NOW + 500,
        expand=True,
        summarize=True,
    )
    assert "chat:" + newer["record_id"] in value["metrics"]["model_input_ids"]
    all_rows = service._saved("phone", value["query_id"])["evidence"]
    later = next(e for e in all_rows if e.get("record_id") == newer["record_id"])
    assert later["person_id"] == "" and later["role"] == "followup"
    assert (
        value["metrics"]["source_requests"] <= 6
        and value["metrics"]["source_records_read"] <= 100
    )
    assert (dump(db.path), dump(cache.path)) == before


def test_changed_context_invalidates_only_affected_answers_and_survives_restart(env):
    service, cache, db, rows, source = env
    service.model = echo
    value = query(service, keyword="本人消息", summarize=True)
    other = query(service, keyword="招物理老师", summarize=True)
    rid = "chat:" + rows[6]["record_id"]
    source.records[6]["record_revision"] = 2
    source.records[6]["text"] = "更正：那条消息不成立。"
    context = service.context("phone", value["query_id"], rid)
    assert context["status"] == "stale"
    stale = service.page("phone", value["query_id"])
    assert stale["model_status"] == "stale" and not stale["claims"]
    assert service.page("phone", other["query_id"])["model_status"] == "available"
    restarted = HistoryRecall(db, service.root, source, unavailable)
    assert not query(restarted, keyword="本人消息")["evidence"]
    cache.receive(source.dataset, rows)
    cache.process()
    assert not query(restarted, keyword="本人消息")["evidence"]
    cache.receive(source.dataset, [source.records[6]])
    cache.process()
    assert query(restarted, keyword="更正")["metrics"]["hits"] == 1


def test_offline_is_not_confirmed_retraction_and_current_revision_wins(env):
    service, cache, db, rows, source = env
    service.model = echo
    value = query(service, keyword="本人消息", summarize=True)
    rid = "chat:" + rows[6]["record_id"]
    source.failure = "MAC_OFFLINE"
    part = service.context("phone", value["query_id"], rid)
    assert part["status"] == "unavailable" and part["evidence"]
    assert service.page("phone", value["query_id"])["model_status"] == "available"
    source.failure = "RECORD_NOT_FOUND"
    part = service.context("phone", value["query_id"], rid)
    assert (
        part["status"] == "stale"
        and service.page("phone", value["query_id"])["model_status"] == "stale"
    )


def test_effective_self_overrides_source_flag_and_unlink_is_unknown(env):
    service, cache, db, rows, source = env
    before = dump(db.path)
    decision(service, rows[6], person="bob-person")
    value = query(service, keyword="本人消息", person_id="bob-person")
    r = next(r for r in value["evidence"] if r["role"] == "match")
    assert r["person_id"] == "bob-person" and r["source_is_from_self"] is True
    assert r["effective_is_self"] is False and r["is_from_self"] is False
    decision(service, rows[6], action="unlink", person="bob-person")
    value = query(service, keyword="本人消息")
    r = next(r for r in value["evidence"] if r["role"] == "match")
    assert (
        r["person_id"] == ""
        and r["effective_is_self"] is None
        and r["source_is_from_self"] is True
    )
    assert dump(db.path) == before


def test_query_intent_does_not_destroy_why_or_proper_names():
    assert plan("当初为什么不选软件工程来着")["intent"] == "reason"
    assert "软件工程" in plan("当初为什么不选软件工程来着")["terms"]
    assert "为什么" not in plan("当初为什么不选软件工程来着")["terms"]
    assert plan("海洋测绘", "海洋测绘")["terms"] == ["海洋测绘"]


def test_old_remote_revision_cannot_replace_current_or_expire_answer(env):
    service, cache, db, rows, source = env
    current = deepcopy(rows[6])
    current.update(record_revision=2, text="本人消息：已改期到周二。")
    cache.receive(source.dataset, [current])
    cache.process()
    service.model = echo
    value = query(service, keyword="本人消息", summarize=True)
    context = service.context(
        "phone", value["query_id"], "chat:" + current["record_id"]
    )
    assert context["status"] == "unavailable"
    assert "SOURCE_REVISION_BEHIND" in context["notice"]
    assert service.page("phone", value["query_id"])["model_status"] == "available"
    cache.receive(source.dataset, [rows[6]])
    cache.process()
    restored = query(service, keyword="本人消息")
    assert (
        next(e for e in restored["evidence"] if e["role"] == "match")["revision"] == 2
    )


def test_frozen_memory_resolves_effective_self_without_mutating_original(env):
    from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
    from allday_asr.v3.adapters.sqlite.cross_day_memory_sources import (
        CrossDayMemorySources,
    )

    service, cache, db, rows, source = env
    legacy = row(rows[6]["text"], self=True)
    legacy["evidence"][0].update(
        dataset=source.dataset,
        record_id=rows[6]["record_id"],
        revision=1,
        sender_account_id=rows[6]["sender_account_id"],
    )
    scope = {"contacts": [], "manifest": [legacy]}
    sources = CrossDayMemorySources(db, lambda: SqliteUnitOfWork(db), cache.path)
    decision(service, rows[6], person="bob-person")
    before = dump(db.path), dump(cache.path)
    projected = sources.load(scope)[0]["evidence"][0]
    assert (
        projected["source_is_from_self"] is True
        and projected["effective_is_self"] is False
    )
    assert projected["person_id"] == "bob-person" and projected["speaker"] == "乙同学"
    assert projected["status"] == "valid"
    assert (dump(db.path), dump(cache.path)) == before
    decision(service, rows[6], action="unlink", person="bob-person")
    projected = sources.load(scope)[0]["evidence"][0]
    assert projected["effective_is_self"] is None and projected["person_id"] == ""
    assert scope["manifest"][0]["evidence"][0]["identity_self"] is True


def test_historical_raw_titles_do_not_create_spurious_revision_conflicts(tmp_path):
    from allday_asr.v3.adapters.sqlite import V3Database, SqliteUnitOfWork
    from allday_asr.v3.application.cross_day_memory import CrossDayMemoryService
    from tests.test_cross_day_memory_v1 import Sources, NOW as memory_now

    class Windows(Sources):
        def freeze(self, first, last, *args):
            scope = deepcopy(self.scope)
            scope.update(first=first, last=last, manifest=self.selected(first))
            return scope

        def selected(self, first):
            if first == "2026-10-05":
                return deepcopy(self.rows)
            recent = deepcopy(self.rows[2])
            recent["id"] = "fictional-new-window"
            recent["evidence"][0].update(
                record_id="new-window",
                reply_record_id=None,
                quote="新窗口讨论另一件独立事情",
            )
            return [recent]

        def load(self, scope):
            ids = {r["id"] for r in scope["manifest"]}
            return [r for r in self.selected(scope["first"]) if r["id"] in ids]

    db = V3Database.open(tmp_path / "memory.sqlite3")
    sources = Windows()
    service = CrossDayMemoryService(
        sources, lambda: SqliteUnitOfWork(db), now=lambda: memory_now
    )
    contacts = [sources.scope["contacts"][0]["id"]]
    service.update("2026-10-05", "2026-10-07", contacts)
    service.update("2026-10-08", "2026-10-10", contacts)
    before = service.get()
    old = service.get(page=1)
    assert len(old["discussions"]) == 3
    for item in sources.rows:
        service.refresh_targets(item["id"], item["id"])
    assert service.get()["revision"] == before["revision"]
    # Actual revised evidence must still invalidate and advance the projection.
    sources.rows[0]["evidence_status"] = "source_changed_or_missing"
    sources.rows[0]["evidence"][0]["status"] = "source_changed_or_missing"
    service.refresh_targets(sources.rows[0]["id"], sources.rows[0]["id"])
    assert service.get()["revision"] > before["revision"]
