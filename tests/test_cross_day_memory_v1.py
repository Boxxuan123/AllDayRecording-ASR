"""All fictional. Real SQLite and cache; no model or speaker pipeline."""

from copy import deepcopy
from datetime import datetime, timezone
import pytest
from tests.chat_fixture import record
from allday_asr.v3.adapters.sqlite import V3Database, SqliteUnitOfWork
from allday_asr.v3.adapters.sqlite.chat_cache import ChatCache
from allday_asr.v3.adapters.sqlite.cross_day_memory_sources import CrossDayMemorySources
from allday_asr.v3.application.cross_day_memory import CrossDayMemoryService
from allday_asr.v3.application.mobile_sync import (
    MobileSyncService,
    UtteranceCorrectionOperationHandler,
)
from allday_asr.v3.domain.ids import stable_ulid, new_ulid
from allday_asr.v3.domain.device_sync import (
    SyncRequest,
    ClientOperation,
    PROJECTION_VERSION,
)

NOW = datetime(2026, 10, 5, tzinfo=timezone.utc)


class Sources:
    def __init__(self):
        p = {
            "id": stable_ulid("fictional-contact"),
            "label": "同名",
            "platform": "qq",
            "person_id": None,
            "identity_basis": "独立来源账号",
        }
        self.scope = {
            "first": "2026-10-05",
            "last": "2026-10-07",
            "timezone": "Asia/Singapore",
            "contacts": [p],
            "coverage": {"missing_sources": []},
            "manifest": [],
            "model_calls": 0,
        }
        self.rows = []
        for n in range(3):
            self.rows.append(
                {
                    "id": stable_ulid("fictional-discussion", str(n)),
                    "title": "虚构招聘机会" + str(n),
                    "contacts": [p],
                    "event_id": stable_ulid("fictional-event") if n < 2 else None,
                    "occurred_at": f"2026-10-0{5 + n}T08:00:00+08:00",
                    "evidence_status": "valid",
                    "pending_change": False,
                    "evidence": [
                        {
                            "source_type": "chat",
                            "revision": 1,
                            "quote": "准备婉拒招聘机会" if n == 1 else "讨论招聘机会",
                            "speaker": "同名",
                            "occurred_at": f"2026-10-0{5 + n}T08:00:00+08:00",
                            "learned_at": None,
                            "assertion": "自述／尚未核实行为",
                            "status": "valid",
                            "record_id": str(n),
                            "dataset": "fictional",
                            "platform": "qq",
                            "source_account_id": "one",
                            "conversation_id": "one",
                            "reply_record_id": "1" if n == 2 else None,
                        }
                    ],
                }
            )

    def freeze(self, *args):
        return deepcopy(self.scope)

    def increment(self, scope):
        return deepcopy(scope)

    def load(self, scope):
        return deepcopy(self.rows)


@pytest.fixture
def loop(tmp_path):
    db = V3Database.open(tmp_path / "core.sqlite3")

    def factory():
        return SqliteUnitOfWork(db, now=lambda: NOW)

    sources = Sources()
    service = CrossDayMemoryService(sources, factory, now=lambda: NOW)
    first = service.update(
        "2026-10-05", "2026-10-07", [sources.scope["contacts"][0]["id"]]
    )
    return db, factory, sources, service, first


def test_trusted_cross_day_distinct_opportunity_topics_and_no_task_mutation(loop):
    db, factory, sources, service, value = loop
    assert len(value["things"]) == 2
    assert len(value["things"][0]["discussion_ids"]) == 2
    assert len(value["people"]) == 1 and len(value["people"][0]["discussion_ids"]) == 3
    assert len(value["topics"][0]["discussion_ids"]) == 3
    assert "准备婉拒" in value["discussions"][1]["evidence"][0]["quote"]
    assert service.get() == value
    assert (
        service.update("2026-10-05", "2026-10-07", [sources.scope["contacts"][0]["id"]])
        == value
    )
    with db.read() as c:
        for table in [
            "event_operations",
            "reminder_schedules",
            "person_memory_entries",
            "user_person_profiles",
        ]:
            if c.execute(
                "SELECT 1 FROM sqlite_master WHERE name=?", (table,)
            ).fetchone():
                assert c.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == 0
        assert (
            c.execute("SELECT COUNT(*) FROM cross_day_index_revisions").fetchone()[0]
            == 1
        )


def test_reject_detach_source_revision_identity_and_repeat(loop):
    db, factory, sources, service, v = loop
    assert len(v["candidates"]) == 1
    c = v["candidates"][0]
    rejected = service.decide(c["source"], c["target"], "reject", v["revision"])
    assert not rejected["candidates"]
    assert service.get() == rejected
    a, b = [r["id"] for r in v["discussions"][:2]]
    detached = service.decide(b, a, "detach", rejected["revision"])
    assert len(detached["things"]) == 3
    assert service.get() == detached
    sources.rows[1]["evidence_status"] = "source_changed_or_missing"
    sources.rows[1]["evidence"][0]["status"] = "source_changed_or_missing"
    sources.rows[0]["contacts"][0]["person_id"] = stable_ulid("corrected-person")
    fresh = service.get()
    assert fresh["discussions"][1]["evidence_status"] != "valid"
    assert fresh["discussions"][1]["thing_id"] == b
    assert not fresh["candidates"]
    with pytest.raises(ValueError):
        service.decide(b, a, "link", fresh["revision"])
    assert CrossDayMemoryService(sources, factory, now=lambda: NOW).get() == fresh
    with db.read() as c:
        assert (
            c.execute("select count(*) from correction_operations").fetchone()[0] == 2
        )


def test_manual_link_sync_replay_stale_conflict_and_receipts(loop):
    db, factory, sources, service, v = loop
    device = stable_ulid("fictional-phone")
    with db.transaction() as c:
        c.execute(
            "INSERT INTO devices VALUES(?,'phone','FICTIONAL','active',1,NULL,NULL,NULL,?,?)",
            (device, NOW.isoformat(), NOW.isoformat()),
        )
    sync = MobileSyncService(
        factory,
        operation_handler=UtteranceCorrectionOperationHandler(now=lambda: NOW),
        now=lambda: NOW,
    )
    sync.memory_refresh = service.refresh_saved
    a, b, d = [r["id"] for r in v["discussions"]]
    op = ClientOperation(
        new_ulid(),
        "memory.link",
        v["revision"],
        {"source": d, "target": a, "action": "link"},
    )
    request = SyncRequest(PROJECTION_VERSION, None, (op,), 500)
    first = sync.synchronize(device, request)
    assert first.receipts[0].status.value == "applied"
    assert len(service.get()["things"]) == 1
    second = sync.synchronize(device, request)
    assert second.receipts == first.receipts
    stale = ClientOperation(
        new_ulid(),
        "memory.link",
        v["revision"],
        {"source": d, "target": a, "action": "reject"},
    )
    assert (
        sync.synchronize(device, SyncRequest(PROJECTION_VERSION, None, (stale,), 500))
        .receipts[0]
        .status.value
        == "conflict"
    )
    sources.rows[2]["evidence_status"] = "source_changed_or_missing"
    changed = ClientOperation(
        new_ulid(),
        "memory.link",
        service.get()["revision"],
        {"source": b, "target": d, "action": "reject"},
    )
    assert (
        sync.synchronize(device, SyncRequest(PROJECTION_VERSION, None, (changed,), 500))
        .receipts[0]
        .status.value
        == "applied"
    )


def test_cache_direct_non_task_same_names_groups_and_original_revision(tmp_path):
    db = V3Database.open(tmp_path / "core.sqlite3")

    def factory():
        return SqliteUnitOfWork(db)

    cache = ChatCache(tmp_path / "chat.sqlite3")
    records = []
    for n, sender, conversation, kind in [
        (1, "alice", "one", "c2c"),
        (2, "bob", "two", "c2c"),
        (3, "alice", "group", "group"),
        (4, "alice", "one", "c2c"),
    ]:
        r = record(
            n,
            "自述擅长初中物理；讨论招聘",
            sender=sender,
            conversation=conversation,
            sent=int(NOW.timestamp()) + n + (86400 if n == 4 else 0),
        )
        r.update(
            conversation_type=kind,
            is_from_self=False,
            self_identity_basis="fictional",
        )
        records.append(r)
    cache.receive("fictional", records)
    cache.process()
    sources = CrossDayMemorySources(db, factory, cache.path)
    inventory = sources.inventory("2026-10-05", "2026-10-07", "Asia/Singapore")
    assert len(inventory["contacts"]) == 2
    assert inventory["contacts"][0]["id"] != inventory["contacts"][1]["id"]
    service = CrossDayMemoryService(sources, factory)
    v = service.update(
        "2026-10-05", "2026-10-07", [p["id"] for p in inventory["contacts"]]
    )
    assert len(v["people"]) == 2 and len(v["discussions"]) == 3
    assert all(p["person_id"] is None for p in v["people"])
    assert not any(
        e["record_id"] == records[2]["record_id"]
        for r in v["discussions"]
        for e in r["evidence"]
    )
    assert len(v["things"]) == 3  # Same account/title/topic alone is insufficient.
    with cache.connect() as c:
        c.execute("UPDATE records SET deleted=1 WHERE id=?", (records[0]["record_id"],))
    changed = service.get()
    assert any(
        r["evidence_status"] == "source_changed_or_missing"
        for r in changed["discussions"]
    )
    assert service.get() == changed


def test_invalid_range_and_contact_budget(loop):
    service = loop[3]
    with pytest.raises(ValueError):
        service.update("2026-10-05", "2026-10-07", ["1"] * 4)
    from allday_asr.v3.domain.cross_day_memory import dates

    with pytest.raises(ValueError):
        dates("2026-10-05", "2026-10-08", "Asia/Singapore")
    with pytest.raises(ValueError):
        dates("2026-10-05", "2026-10-07", "UTC")


def test_root_detach_review_inbox_and_immutable_history(loop):
    db, factory, sources, service, v = loop
    with factory().reading() as u:
        items = u.desktop.list_reviews(50)
    association = [
        i for i in items if i["reason"] == "memory_association_requires_confirmation"
    ]
    assert len(association) == 1
    assert association[0]["context"]["memory_association"]["revision"] == v["revision"]
    a, b, d = [r["id"] for r in v["discussions"]]
    linked = service.decide(d, a, "link", v["revision"])
    # Detaching the original destination must isolate that record too.
    detached = service.decide(a, b, "detach", linked["revision"])
    assert next(r for r in detached["discussions"] if r["id"] == a)["thing_id"] == a
    assert next(r for r in detached["discussions"] if r["id"] == d)["thing_id"] != a
    with db.read() as c:
        assert (
            c.execute("SELECT COUNT(*) FROM cross_day_index_revisions").fetchone()[0]
            == 3
        )
    with factory().reading() as u:
        assert not [
            i
            for i in u.desktop.list_reviews(50)
            if i["reason"] == "memory_association_requires_confirmation"
        ]


def test_migration_33_preserves_existing_sync_and_decisions(tmp_path):
    from allday_asr.v3.adapters.sqlite.migration_runner import V3MigrationRunner
    from allday_asr.v3.adapters.sqlite.migrations import MIGRATIONS
    import sqlite3

    path = tmp_path / "old.sqlite3"
    V3MigrationRunner(
        path, migrations=[m for m in MIGRATIONS if m.version <= 33]
    ).initialize()
    c = sqlite3.connect(path)
    c.execute(
        "INSERT INTO devices VALUES(?,'phone','FICTIONAL','active',1,NULL,NULL,NULL,?,?)",
        ("fictional", NOW.isoformat(), NOW.isoformat()),
    )
    c.execute(
        "INSERT INTO sync_cursors VALUES(?,?,?,?)",
        ("fictional", 6, 123, NOW.isoformat()),
    )
    c.commit()
    c.close()
    db = V3Database.open(path)
    with db.read() as c:
        assert tuple(
            c.execute(
                "SELECT projection_version,acknowledged_sequence FROM sync_cursors"
            ).fetchone()
        ) == (7, 123)
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not c.execute("PRAGMA foreign_key_check").fetchall()
    assert db.initialize() == 35


def test_unknown_recording_candidate_person_never_becomes_known_interaction(tmp_path):
    # Fictional source snapshots: an unresolved candidate may carry a person ID,
    # while a separate, confirmed recording makes that person selectable.
    person = {"id": stable_ulid("fictional-known"), "person_id": stable_ulid("fictional-known"),
              "platform": "recording", "label": "虚构已确认人物", "identity_basis": "人工确认"}
    events = []
    for n, kind, status in [(0, "unknown", "unresolved"), (1, "known", "confirmed")]:
        events.append({"id": stable_ulid("fictional-recorded", str(n)), "revision": 1,
                       "title": "招聘讨论", "evidence": [{"revision": 1, "text": "讨论招聘机会",
                       "start_at": f"2026-10-0{5+n}T08:00:00+08:00", "session_id": str(n),
                       "utterance_id": str(n), "start_ms": 0, "end_ms": 1000,
                       "participant": {"person_id": person["id"], "kind": kind, "status": status,
                                       "label": "声纹候选不可作为身份依据"}}]})
    sources = CrossDayMemorySources(None, None, tmp_path / "absent.sqlite3")
    sources._read = lambda *args: ([], {person["id"]: person}, {}, events, {}, {"missing_sources": []})
    scope = sources.freeze("2026-10-05", "2026-10-07", "Asia/Singapore", [person["id"]])
    unknown, known = scope["manifest"]
    assert unknown["contacts"] == []
    assert unknown["evidence"][0]["speaker"] == "未知说话人 · 录音中出现的讨论"
    assert known["contacts"] == [person]
    from allday_asr.v3.domain.cross_day_memory import project
    value = project(scope, scope["manifest"], [])
    assert value["people"][0]["discussion_ids"] == [known["id"]]
    assert len(value["topics"][0]["discussion_ids"]) == 2
    assert len(value["things"]) == 2
