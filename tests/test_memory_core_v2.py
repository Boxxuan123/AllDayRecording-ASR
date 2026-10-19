"""No-model invariants. Synthetic checks are not semantic evaluation scores."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
import pytest
from allday_asr.memory_v2.contracts import SourceRecord, Grant
from allday_asr.memory_v2.store import SourceStore
from allday_asr.memory_v2.budget import Ledger, BudgetExceeded
from allday_asr.memory_v2.raw import tokenize
from allday_asr.memory_v2.core import MemoryCore


def record(i=1, **kw):
    values = dict(
        platform="qq",
        source_account_id="owner",
        conversation_id="room",
        sender_account_id="sender",
        record_id=str(i),
        revision=1,
        sent_at=100 + i,
        timezone="Asia/Singapore",
        text="明晚报名 API更新",
        raw_text="明晚报名 API更新",
        is_from_self=False,
        speaker="小明",
    )
    return SourceRecord(**(values | kw))


def test_namespace_is_not_nickname():
    r = record()
    assert r.key != replace(r, platform="wechat").key
    assert r.actor != replace(r, source_account_id="other").actor
    assert r.key != replace(r, conversation_id="other").key


def test_grant_is_half_open_and_keeps_platforms():
    g = Grant(frozenset({("qq", "owner"), ("wechat", "owner")}), 100, 200)
    assert g.allows(record(platform="wechat"))
    assert not g.allows(record(sent_at=200))
    assert not g.allows(record(source_account_id="other"))
    assert not g.allows(record(deleted=True))


def test_incremental_revision_and_withdrawal(tmp_path):
    s = SourceStore(tmp_path)
    r = record()
    assert s.update([r]) == {r.key}
    eps, deleted = s.plan()
    s.ready(eps[0])
    assert s.update([r]) == set()
    assert s.plan() == ([], [])
    s.update([replace(r, revision=2, text="报名取消", raw_text="报名取消")])
    assert s.episode(eps[0]["id"]) is None
    revised, _ = s.plan()
    s.ready(revised[0])
    assert revised[0]["id"] == eps[0]["id"]
    s.update([replace(r, revision=3, deleted=True)])
    assert s.episode(eps[0]["id"]) is None
    assert s.plan()[1] == [eps[0]["id"]]


def test_revision_conflicts_are_not_overwritten(tmp_path):
    s = SourceStore(tmp_path)
    r = record(revision=2)
    s.update([r])
    with pytest.raises(ValueError):
        s.update([replace(r, text="改写")])
    with pytest.raises(ValueError):
        s.update([replace(r, revision=1)])
    assert s.get(r.key) == r


def test_lexical_chinese_mixed():
    t = tokenize("明晚报名，API v2更新")
    assert "报名" in t and "api" in t and "v2" in t
    assert "报名" in tokenize("报名")


def test_directory_order_and_rebuild_renaming(tmp_path):
    rows = [record(i, conversation_id="r" + str(i % 2)) for i in range(1, 40)]
    a = SourceStore(tmp_path / "a")
    b = SourceStore(tmp_path / "b")
    a.update(rows)
    b.update(rows[::-1])
    assert a.build_episodes() == b.build_episodes()
    renamed = [
        replace(
            r,
            speaker="新称呼",
            sender_account_id="new-id",
            text=r.text.replace("报名", "登记"),
            raw_text=r.raw_text.replace("报名", "登记"),
        )
        for r in rows
    ]
    c = SourceStore(tmp_path / "c")
    c.update(renamed)
    assert {e["fingerprint"] for e in a.build_episodes()}.isdisjoint(
        {e["fingerprint"] for e in c.build_episodes()}
    )


def test_budget_reserves_unknown_and_serializes(tmp_path):
    ledger = Ledger(tmp_path, max_calls=2, max_tokens=100)
    one = ledger.reserve("retain", 60)
    ledger.finish(one, error="timeout")
    with pytest.raises(BudgetExceeded):
        ledger.reserve("answer", 50)
    two = ledger.reserve("answer", 40)
    ledger.finish(two, {"input_tokens": 10, "output_tokens": 5})
    report = ledger.export()
    assert report["known_tokens"] == 15 and report["accounted_tokens"] == 75
    assert report["unknown_usage_count"] == 1
    with pytest.raises(BudgetExceeded):
        Ledger(tmp_path, max_calls=2, max_tokens=100).reserve("answer", 1)


def test_invalid_claim_removed_locally(tmp_path):
    s = SourceStore(tmp_path)
    r = record()
    s.update([r])

    class M:
        def call(self, *args):
            return {
                "claims": [
                    {
                        "text": "有报名发言",
                        "kind": "statement",
                        "citations": [{"handle": "E1", "quote": "报名"}],
                    },
                    {
                        "text": "虚假",
                        "kind": "statement",
                        "citations": [{"handle": "E9", "quote": "已执行"}],
                    },
                ],
                "unknown": [],
            }

    core = MemoryCore(s, SimpleNamespace(bank="same-bank"), M())
    result = asyncio.run(
        core.answer(
            "说了什么",
            120,
            Grant(frozenset({("qq", "owner")}), 0, 200),
            retrieved={"records": [r], "plan": {}, "traces": []},
        )
    )
    assert len(result["claims"]) == 1 and len(result["rejected_claims"]) == 1
    assert result["claims"][0]["citations"][0]["revision"] == 1


def test_midflight_withdrawal_invalidates_answer(tmp_path):
    s = SourceStore(tmp_path)
    r = record()
    s.update([r])

    class M:
        def call(self, *args):
            s.update([replace(r, revision=2, deleted=True)])
            return {
                "claims": [
                    {
                        "text": "报名",
                        "kind": "statement",
                        "citations": [{"handle": "E1", "quote": "报名"}],
                    }
                ],
                "unknown": [],
            }

    core = MemoryCore(s, SimpleNamespace(bank="same-bank"), M())
    result = asyncio.run(
        core.answer(
            "说了什么",
            120,
            Grant(frozenset({("qq", "owner")}), 0, 200),
            retrieved={"records": [r], "plan": {}, "traces": []},
        )
    )
    assert not result["claims"]


def test_unauthorized_source_never_enters_model(tmp_path):
    s = SourceStore(tmp_path)
    r = record(source_account_id="forbidden")
    s.update([r])

    class M:
        def call(self, phase, payload, *args):
            assert payload["records"] == []
            return {"claims": [], "unknown": ["无授权证据"]}

    core = MemoryCore(s, SimpleNamespace(bank="same-bank"), M())
    result = asyncio.run(
        core.answer(
            "说了什么",
            120,
            Grant(frozenset({("qq", "owner")}), 0, 200),
            retrieved={"records": [r], "plan": {}, "traces": []},
        )
    )
    assert result["input_count"] == 0


def test_schema_refs_reject_before_generation():
    from allday_asr.memory_v2.model import validate_schema_refs

    with pytest.raises(KeyError):
        validate_schema_refs(
            {"type": "object", "properties": {"x": {"$ref": "#/$defs/missing"}}}
        )
    validate_schema_refs(
        {"$defs": {"x": {"type": "string"}}, "properties": {"x": {"$ref": "#/$defs/x"}}}
    )


def test_frozen_directory_retains_native_labels():
    from allday_asr.memory_v2.frozen import FrozenSnapshot
    from tests.chat_fixture import record as native

    r = native(1, "报名", sender="alice")
    r["sender_display_name"] = "自然称呼"
    r["conversation_type"] = "direct"
    f = FrozenSnapshot(
        {"records": [r], "dataset_id": "d", "source_status": {"status": "ready"}}
    )
    assert any(
        a["display_name"] == "自然称呼" for a in f.request("/v1/accounts")["items"]
    )
    assert f.request("/v1/conversations")["items"][0]["conversation_type"] == "direct"


def test_frozen_directory_accepted_by_baseline_contract():
    from allday_asr.memory_v2.frozen import FrozenSnapshot
    from allday_asr.v3.adapters.sqlite.recall_directory import entry
    from tests.chat_fixture import record as native

    r = native(1, "报名")
    r["conversation_type"] = "group"
    source = FrozenSnapshot({"records": [r], "dataset_id": "d", "source_status": {}})
    for value in source.request("/v1/conversations")["items"]:
        assert entry(value, "d") is not None


def test_fragment_selection_preserves_turns_and_excludes_future(tmp_path):
    from allday_asr.memory_v2.selection import fragment_rows, pack_fragments, room_key

    s = SourceStore(tmp_path)
    rows = [
        record(i, conversation_id="a" if i % 2 else "b", sent_at=100 + i)
        for i in range(1, 9)
    ]
    s.update(rows)
    allowed = [r for r in rows if r.sent_at <= 106]
    task = {
        "query": "计划",
        "conversation_keys": [room_key(rows[0])],
        "message_start": 101,
        "message_end": 106,
    }
    pieces = fragment_rows(s.build_episodes(), s, allowed, task)
    packed = pack_fragments(pieces)
    found = [r for f in packed for r in f["rows"]]
    assert [r.sent_at for r in found] == [101, 103, 105]
    assert all(r.conversation_id == "a" for r in found)


def test_event_window_does_not_exclude_daytime_plan():
    from allday_asr.memory_v2.selection import accepts

    assert accepts(record(sent_at=120), {"event_start": 180, "event_end": 200})
    assert not accepts(record(sent_at=120), {"message_start": 180, "message_end": 200})


def test_unknown_object_selector_rejected():
    from allday_asr.memory_v2.selection import validate_task

    with pytest.raises(ValueError, match="UNKNOWN_CONVERSATION"):
        validate_task(
            {"query": "查原因", "conversation_keys": ["invented"]}, {"real"}, 200
        )


def test_focus_drops_revised_and_unauthorized_sources(tmp_path):
    s = SourceStore(tmp_path)
    r = record()
    s.update([r])
    core = MemoryCore(s, SimpleNamespace(), SimpleNamespace())
    focus = {"source_keys": [r.key], "revisions": {r.key: 1}}
    grant = Grant(frozenset({("qq", "owner")}), 0, 200)
    assert core.valid_focus(focus, grant, 150)["source_keys"] == [r.key]
    s.update([replace(r, revision=2)])
    assert core.valid_focus(focus, grant, 150)["source_keys"] == []


def test_valid_quotes_are_not_semantic_entailment(tmp_path):
    # Deliberate counterexample: the citation verifier proves provenance, not causality.
    # Keep this limitation visible rather than calling an invalid handle a relation test.
    s = SourceStore(tmp_path)
    a = record(1, text="准备参加", raw_text="准备参加")
    b = record(2, text="结果未公布", raw_text="结果未公布")
    s.update([a, b])

    class M:
        def call(self, *args):
            return {
                "claims": [
                    {
                        "text": "他已经获奖",
                        "kind": "completed_report",
                        "citations": [
                            {"handle": "E1", "quote": "准备参加"},
                            {"handle": "E2", "quote": "结果未公布"},
                        ],
                    }
                ],
                "unknown": [],
            }

    core = MemoryCore(s, SimpleNamespace(bank="synthetic"), M())
    result = asyncio.run(
        core.answer(
            "结果如何",
            150,
            Grant(frozenset({("qq", "owner")}), 0, 200),
            retrieved={"records": [a, b], "plan": {}, "traces": []},
        )
    )
    assert len(result["claims"]) == 1  # Explicit known gap, not a semantic pass.


def test_query_entry_persists_state_and_enforces_deadline(tmp_path):
    import time

    s = SourceStore(tmp_path)
    r = record()
    s.update([r])

    class M:
        def call(self, *args):
            return {"claims": [], "unknown": ["未证实"], "retrieval_actions": []}

    core = MemoryCore(s, SimpleNamespace(bank="test"), M())

    async def retrieve(*args, **kwargs):
        return {"records": [r], "plan": {}, "traces": []}

    core.retrieve = retrieve
    g = Grant(frozenset({("qq", "owner")}), 0, 200)
    result = asyncio.run(core.query("发生什么", 150, g))
    assert (core.audit / result["query_id"] / "state.json").exists()

    async def slow(*args, **kwargs):
        await asyncio.sleep(0.1)

    core.retrieve = slow
    with pytest.raises(TimeoutError):
        asyncio.run(core.query("发生什么", 150, g, deadline=time.monotonic() + 0.01))


def test_evidence_gap_fetches_once_then_answers(tmp_path):
    from allday_asr.memory_v2.selection import room_key

    s = SourceStore(tmp_path)
    a = record(1, text="明天再确认", raw_text="明天再确认")
    b = record(2, text="已经取消", raw_text="已经取消")
    s.update([a, b])

    class M:
        calls = 0

        def call(self, phase, payload, *args):
            self.calls += 1
            if self.calls == 1:
                return {
                    "claims": [],
                    "unknown": ["缺后续"],
                    "retrieval_actions": [
                        {
                            "query": "取消或确认的后续",
                            "conversation_keys": [room_key(a)],
                            "message_start": 100,
                            "message_end": 150,
                            "purpose": "followup",
                            "basis": "E1 表示等待确认",
                        }
                    ],
                }
            assert any(r["text"] == "已经取消" for r in payload["records"])
            return {
                "claims": [
                    {
                        "text": "后续说取消",
                        "kind": "statement",
                        "citations": [{"handle": "E2", "quote": "已经取消"}],
                    }
                ],
                "unknown": [],
                "retrieval_actions": [],
            }

    m = M()
    core = MemoryCore(s, SimpleNamespace(bank="synthetic"), m)
    fetches = []

    async def retrieve(*args, **kwargs):
        fetches.append(kwargs["planned"])
        return {"records": [b], "plan": kwargs["planned"], "traces": []}

    core.retrieve = retrieve
    g = Grant(frozenset({("qq", "owner")}), 0, 200)
    r = asyncio.run(
        core.answer(
            "后来呢", 150, g, retrieved={"records": [a], "plan": {}, "traces": []}
        )
    )
    assert m.calls == 2 and len(fetches) == 1 and len(r["claims"]) == 1
    assert len(list(core.audit.glob("*/continued.json"))) == 1


def test_stage_policy_cannot_be_relaxed_by_reopening(tmp_path):
    ledger = Ledger(tmp_path, max_calls=1, max_tokens=50)
    ledger.finish(ledger.reserve("first", 20), {"input_tokens": 1, "output_tokens": 1})
    reopened = Ledger(tmp_path, max_calls=48, max_tokens=800000)
    reopened.max_calls = 48
    with pytest.raises(BudgetExceeded):
        reopened.reserve("second", 1)


def test_reworded_basis_cannot_repeat_same_search(tmp_path):
    s = SourceStore(tmp_path)
    r = record()
    s.update([r])
    task = {
        "query": "后续确认",
        "conversation_keys": [],
        "message_start": None,
        "message_end": None,
        "event_start": None,
        "event_end": None,
    }

    class M:
        def call(self, *args):
            return {
                "claims": [
                    {
                        "text": "提到报名",
                        "kind": "statement",
                        "citations": [{"handle": "E1", "quote": "报名"}],
                    }
                ],
                "unknown": [],
                "retrieval_actions": [
                    {**task, "basis": "E1 换一个理由再次查询", "purpose": "followup"}
                ],
            }

    core = MemoryCore(s, SimpleNamespace(bank="test"), M())

    async def forbidden(*args, **kwargs):
        raise AssertionError("duplicate search executed")

    core.retrieve = forbidden
    result = asyncio.run(
        core.answer(
            "后来呢",
            150,
            Grant(frozenset({("qq", "owner")}), 0, 200),
            retrieved={"records": [r], "plan": {"tasks": [task]}, "traces": []},
        )
    )
    assert len(result["claims"]) == 1 and len(result["rejected_actions"]) == 1


def test_planning_cannot_see_future_or_unauthorized_previews(tmp_path):
    s = SourceStore(tmp_path)
    rows = [
        record(),
        record(2, source_account_id="forbidden", text="秘密"),
        record(3, sent_at=180, text="未来"),
    ]
    s.update(rows)

    class M:
        def call(self, phase, payload, *args):
            import json

            assert phase == "plan"
            data = json.dumps(payload, ensure_ascii=False)
            assert "秘密" not in data and "未来" not in data and "forbidden" not in data
            return {
                "tasks": [{"query": "报名", "event_start": None, "event_end": None}],
                "unknown": [],
            }

    class B:
        async def retrieve(self, *args):
            return {}

    core = MemoryCore(s, B(), M())
    result = asyncio.run(
        core.retrieve("发生什么", 150, Grant(frozenset({("qq", "owner")}), 0, 200))
    )
    assert result["records"] == []
