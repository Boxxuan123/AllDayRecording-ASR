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
