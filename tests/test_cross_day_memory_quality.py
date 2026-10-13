"""Synthetic content/security regression; provider calls are never configured."""
import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timezone
from tests.chat_fixture import record
from tests.test_cross_day_memory_v1 import Sources, NOW
from allday_asr.v3.domain.cross_day_memory import project
from allday_asr.v3.domain.memory_content import safe_memory
from allday_asr.v3.domain.chat_projection import semantic_text
from allday_asr.v3.adapters.sqlite import V3Database, SqliteUnitOfWork
from allday_asr.v3.adapters.sqlite.chat_cache import ChatCache
from allday_asr.v3.adapters.sqlite.cross_day_memory_sources import CrossDayMemorySources
from allday_asr.v3.application.cross_day_memory import CrossDayMemoryService
from allday_asr.v3.application.mobile_sync import MobileSyncService
from allday_asr.v3.domain.device_sync import SyncRequest, PROJECTION_VERSION

SECRET = "SYNTHETIC_PRIVATE_ACCESS"
IMAGE = '<msg><img aeskey="'+SECRET+'" cdnthumbaeskey="'+SECRET+'" cdnthumburl="https://files.example/download?token='+SECRET+'"/></msg>'


def row(quote, self=False):
    r = deepcopy(Sources().rows[2])
    r["event_id"] = None
    r["evidence"][0].update(quote=quote, identity_self=self, sender_account_id="fictional-peer", reply_record_id=None)
    return r


def test_promotion_image_and_real_price_interest_without_account_labels():
    source = Sources()
    promo = row("国庆购机活动最后一天，库存不多，尽快到店享优惠")
    image = row(IMAGE)
    for r in [promo, image, row("你好")]:
        v = project(source.scope, [r], [])
        assert not v["things"] and not v["people"] and len(v["excluded"]) == 1
        assert SECRET not in json.dumps(v)
    engaged = deepcopy(promo)
    response = deepcopy(engaged["evidence"][0]); response.update(quote="这款型号多少钱？我想买一台", identity_self=True, record_id="self")
    engaged["evidence"].append(response)
    v = project(source.scope, [engaged], [])
    assert len(v["people"]) == len(v["things"]) == 1
    assert v["people"][0]["identity_basis"] == source.scope["contacts"][0]["identity_basis"]
    response["quote"] = "谢谢"
    assert not project(source.scope, [engaged], [])["things"]
    # Meaningful parsed content participates; the raw attachment alone cannot.
    parsed = row('<msg><img aeskey="'+SECRET+'"/><text>讨论物理教师招聘</text></msg>')
    assert project(source.scope, [parsed], [])["things"]


def test_generic_content_and_last_evidence_not_first_or_sync():
    s=Sources()
    a=row('木笛样品暂不寄出，先核对地址。');a.update(id='a',occurred_at='2026-10-05T00:35:57Z');a['evidence'][0]['occurred_at']='2026-10-05T00:36:13Z'
    b=row('木笛样品已改期，等下周再寄。');b.update(id='b',occurred_at='2026-10-05T01:22:21Z');b['evidence'][0]['occurred_at']='2026-10-05T01:23:36Z'
    last=deepcopy(b['evidence'][0]);last.update(quote='尚未交给快递员。',occurred_at='2026-10-05T01:42:18Z',identity_self=True);b['evidence'].append(last)
    unrelated=row('榕树修剪由园艺队处理。');unrelated.update(id='other',contacts=[])
    v=project(s.scope,[a,b,unrelated],[])
    assert v['people'][0]['last_evidence_at']=='2026-10-05T01:42:18Z'
    assert len(v['things'])==3 and len(v['candidates'])==1
    assert '木笛样品' in v['candidates'][0]['reason'] and '人工确认' in v['candidates'][0]['reason']
    assert '尚未交给快递员。' in v['discussions'][1]['summary'] and '不推断事实状态' in v['discussions'][1]['summary']
    assert any(t['discussion_ids']==['a','b'] for t in v['topics'])
    for action in ['reject','detach']:
        assert not project(s.scope,[a,b],[{'source':'a','target':'b','action':action}])['candidates']
    new=deepcopy(b);new['evidence'][0]['quote']='另有一件事，木笛样品只是参考。'
    assert not project(s.scope,[a,new],[])['candidates']


def test_sources_safe_before_truncation_and_original_lookup_preserved(tmp_path):
    db = V3Database.open(tmp_path / "core.sqlite3"); cache = ChatCache(tmp_path / "chat.sqlite3")
    r = record(1, IMAGE, sent=int(NOW.timestamp())); r.update(conversation_type="c2c", is_from_self=False)
    cache.receive("fictional", [r]); cache.process()
    factory = lambda: SqliteUnitOfWork(db)
    sources = CrossDayMemorySources(db, factory, cache.path)
    inv = sources.inventory("2026-10-05", "2026-10-07", "Asia/Singapore")
    scope = sources.freeze("2026-10-05", "2026-10-07", "Asia/Singapore", [inv["contacts"][0]["id"]])
    assert scope["manifest"] == []
    # Legacy frozen image rows still validate by revision, without exposing raw XML.
    legacy=row(IMAGE); legacy["evidence"][0].update(dataset="fictional",record_id=r["record_id"],revision=1,sender_account_id=r["sender_account_id"],identity_self=False)
    scope["manifest"]=[safe_memory(legacy)]
    assert scope["manifest"][0]["evidence"][0]["quote"] == "收到图片，内容未解析"
    assert SECRET not in json.dumps(scope)  # input/export/log-safe projection
    loaded = sources.load(scope)
    assert loaded[0]["evidence_status"] == "valid"
    service = CrossDayMemoryService(sources, factory)
    value = service.update("2026-10-05", "2026-10-07", [inv["contacts"][0]["id"]])
    assert value["status"] == "no_data" and service.get() == value
    with cache.connect() as c:
        body = json.loads(c.execute("select body from records where id=?", (r["record_id"],)).fetchone()[0])
        assert body["text"] == IMAGE
    assert semantic_text("查看 https://files.example/download?token="+SECRET) == "查看 https://files.example/download"


def test_security_migration_and_old_sync_replay_sanitize_only_derived(tmp_path):
    from allday_asr.v3.adapters.sqlite.migration_runner import V3MigrationRunner
    from allday_asr.v3.adapters.sqlite.migrations import MIGRATIONS
    path = tmp_path / "core.sqlite3"
    V3MigrationRunner(path, migrations=[m for m in MIGRATIONS if m.version <= 34]).initialize()
    c = sqlite3.connect(path)
    source = Sources();source.rows[0]["evidence"][0]["quote"] = IMAGE
    # Simulate old immutable memory snapshots written before the projection fix.
    scope = source.scope; scope["manifest"] = source.rows
    value = {"discussions": source.rows, "id": "fixture", "revision": 1}
    c.execute("INSERT INTO cross_day_index_revisions VALUES(1,?,?,?)", (json.dumps(scope),json.dumps(value),NOW.isoformat()))
    c.execute("INSERT INTO change_events(resource_type,resource_id,revision,operation,payload_json,created_at) VALUES('memory_index','fixture',1,'upsert',?,?)", (json.dumps(value),NOW.isoformat()))
    c.execute("INSERT INTO cross_day_link_decisions VALUES('a','b','reject')")
    c.execute("INSERT INTO devices VALUES('phone','phone','FICTIONAL','active',1,NULL,NULL,NULL,?,?)", (NOW.isoformat(), NOW.isoformat()))
    c.commit();c.close()
    db = V3Database.open(path)
    with db.read() as c:
        saved = c.execute("select scope_json,value_json from cross_day_index_revisions").fetchone()
        assert SECRET not in "".join(saved)
        assert SECRET not in c.execute("select payload_json from change_events where resource_type='memory_index'").fetchone()[0]
        assert c.execute("select action from cross_day_link_decisions").fetchone()[0] == "reject"
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    factory = lambda: SqliteUnitOfWork(db)
    # Read projection also fences old change-log copies without trusting their age.
    with factory() as u:
        u.changes.append("memory_index", "fixture", 1, "upsert", value)
    sync = MobileSyncService(factory)
    response = sync.synchronize("phone", SyncRequest(PROJECTION_VERSION, None, (), 500))
    assert SECRET not in json.dumps(response.as_dict())
    assert db.initialize() == 35
    assert safe_memory(safe_memory(scope)) == safe_memory(scope)


def test_original_context_api_projects_attachments_and_keeps_source_id():
    from types import SimpleNamespace
    from allday_asr.v3.interfaces.desktop_chat_routes import DesktopChatRoutesMixin
    class Handler(DesktopChatRoutesMixin):
        application = SimpleNamespace(chat=SimpleNamespace(context=lambda *args: {"items": [{"record_id": "fictional-original", "text": IMAGE, "aeskey": SECRET}]}))
        def _followup_get(self, *args): return False
        def _send_json(self, status, value): self.value = value
    h = Handler()
    assert h._dispatch_chat_get("/api/v3/chat/context", {"record_id": ["fictional-original"]})
    assert SECRET not in json.dumps(h.value)
    assert h.value["items"][0]["record_id"] == "fictional-original"
    assert h.value["items"][0]["text"] == "收到图片，内容未解析"


def test_physics_topic_does_not_invent_received_message_or_ability():
    from allday_asr.v3.domain.memory_content import content
    title,summary=content(row("正在招聘物理老师，准备婉拒；初中物理不擅长，高中物理也不会"))
    assert "收到" not in summary and "较有把握" not in summary
    assert "不推断事实状态" in summary and "初中物理不擅长" in summary
