"""Fictional bounded cache arrivals; no providers, tasks or real sources."""
from copy import deepcopy
import json
from tests.chat_fixture import record
from tests.test_cross_day_memory_quality import IMAGE, SECRET
from tests.test_cross_day_memory_v1 import NOW
from allday_asr.v3.adapters.sqlite import V3Database, SqliteUnitOfWork
from allday_asr.v3.adapters.sqlite.chat_cache import ChatCache
from allday_asr.v3.adapters.sqlite.cross_day_memory_sources import CrossDayMemorySources
from allday_asr.v3.application.cross_day_memory import CrossDayMemoryService


def setup(tmp_path):
    db=V3Database.open(tmp_path/'core.sqlite3'); cache=ChatCache(tmp_path/'chat.sqlite3')
    factory=lambda:SqliteUnitOfWork(db)
    sources=CrossDayMemorySources(db,factory,cache.path)
    service=CrossDayMemoryService(sources,factory,now=lambda:NOW)
    def arrive(i,text,offset=0,self=False,revision=1,reply=None):
        r=record(i,text,sent=int(NOW.timestamp())+offset,conversation='peer',revision=revision)
        r.update(conversation_type='c2c',is_from_self=self,reply={'record_id':'rec:fixture-'+str(reply)} if reply is not None else None)
        cache.receive('fictional',[r]);cache.process();return r
    def update():
        contact=sources.inventory('2026-10-05','2026-10-07','Asia/Singapore')['contacts'][0]['id']
        return service.update('2026-10-05','2026-10-07',[contact])
    return db,cache,sources,service,arrive,update


def test_same_scope_new_interaction_explicit_update_and_stable_evidence(tmp_path):
    db,cache,sources,service,arrive,update=setup(tmp_path)
    arrive(1,'讨论新的物理教师招聘，尚未回复')
    first=update();firstid=first['discussions'][0]['id']
    arrive(2,'我准备考虑一下，尚未实际发送回复',60,True,reply=1)
    assert service.get()==first # reads never ingest new cached originals
    second=update();assert second['revision']==first['revision']+1
    assert second['discussions'][0]['id']==firstid
    assert len(second['discussions'][0]['evidence'])==2
    assert second['people'][0]['last_evidence_at']=='2026-10-05T00:01:00Z'
    assert update()==service.get()==second
    assert '已完成' not in second['things'][0]['progress']
    arrive(1,'修订后原文',revision=2)
    invalid=update();assert invalid['discussions'][0]['evidence_status']=='source_changed_or_missing'
    assert invalid['discussions'][0]['evidence'][0]['revision']==1
    assert update()==invalid


def test_incremental_admission_reconsider_interest_and_routine_rewards(tmp_path):
    db,cache,sources,service,arrive,update=setup(tmp_path)
    arrive(1,'国庆购机活动到店享优惠')
    empty=update();assert not empty['things']
    arrive(2,IMAGE,20);assert not update()['things']
    arrive(3,'这款型号多少钱？我想买一台',40,True,reply=1)
    value=update();assert len(value['things'])==1
    assert value['discussions'][0]['evidence'][1]['quote']=='收到图片，内容未解析'
    assert SECRET not in json.dumps(value)
    arrive(4,'签到',4000,True);arrive(5,'签到成功，本次获得40积分，当前积分200',4010,reply=4)
    value=update();assert len(value['things'])==1
    assert value['scope']['coverage']['incremental']['excluded_discussions']==1
    assert update()==value
    arrive(6,'签到积分扣错了，请帮我核对规则',4020,True)
    assert len(update()['things'])==2 # real inquiry is useful; no permanent account label


def test_capacity_decisions_restart_replay_and_no_eviction(tmp_path):
    db,cache,sources,service,arrive,update=setup(tmp_path)
    arrive(1,'讨论第一次招聘机会',0);arrive(2,'讨论另一次招聘机会',4000)
    initial=update();a,b=[r['id'] for r in initial['discussions']]
    value=service.decide(a,b,'link',initial['revision'])
    value=service.decide(a,b,'detach',value['revision'])
    value=service.decide(a,b,'reject',value['revision'])
    for i in range(3,10): arrive(i,'讨论独立机会编号'+str(i),4000*(i-1))
    value=update();assert len(value['discussions'])==7
    assert value['scope']['coverage']['incremental']['deferred_discussions']==0
    assert value['scope']['coverage']['incremental']['processed_discussions']==9
    assert len(service.get(page=1)['discussions'])==2
    assert {a,b}<={r['id'] for r in value['discussions']}
    assert len({r['thing_id'] for r in value['discussions']})==7
    assert not value['candidates']
    with db.read() as c:
        decisions=[tuple(r) for r in c.execute('select * from cross_day_link_decisions')]
        changes=c.execute("select count(*) from change_events where resource_type='memory_index'").fetchone()[0]
    restarted=CrossDayMemoryService(sources,lambda:SqliteUnitOfWork(db),now=lambda:NOW)
    assert restarted.update('2026-10-05','2026-10-07',[value['scope']['contacts'][0]['id']])==value
    assert update()==value
    with db.read() as c:
        assert [tuple(r) for r in c.execute('select * from cross_day_link_decisions')]==decisions
        assert c.execute("select count(*) from change_events where resource_type='memory_index'").fetchone()[0]==changes
        assert c.execute('select count(*) from reminder_schedules').fetchone()[0]==0


def test_opaque_login_strings_safe_memory_but_raw_source_retained(tmp_path):
    db,cache,sources,service,arrive,update=setup(tmp_path)
    arrive(1,'https://login.example/view')
    arrive(2,'13900000000',1)
    arrive(3,'@Fictional12345',2,True)
    arrive(4,'请帮忙选择照片，登录需要绑定微信',3)
    value=update();r=value['discussions'][0]
    assert r['title']=='原文：请帮忙选择照片，登录需要绑定微信'
    assert '13900000000' not in json.dumps(value) and '@Fictional12345' not in json.dumps(value)
    assert update()==value
    with cache.connect() as c:
        assert '@Fictional12345' in c.execute("select body from records where id='rec:fixture-3'").fetchone()[0]
