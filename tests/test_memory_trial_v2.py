"""Fictional live source only; no production DB, providers or model calls."""
import time
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
import pytest
from tests.chat_fixture import FixtureMac,record
from allday_asr.v3.adapters.sqlite import V3Database,SqliteUnitOfWork
from allday_asr.v3.adapters.sqlite.chat_cache import ChatCache
from allday_asr.v3.adapters.sqlite.cross_day_memory_sources import CrossDayMemorySources
from allday_asr.v3.application.cross_day_memory import CrossDayMemoryService
from allday_asr.v3.application.chat_queries import ChatQueries
from allday_asr.v3.bootstrap.memory_trial import compose_memory_trial as MemoryTrial
from allday_asr.v3.adapters.sqlite.memory_trial_cache import TRIAL_KEY

def setup(tmp_path):
    now=int(time.time());clock=[float(now)]
    def rec(i,text,offset=0,self=False,revision=1):
        r=record(i,text,conversation='peer',sent=now+offset,revision=revision)
        r.update(conversation_type='c2c',is_from_self=self);return r
    baseline=rec(1,'合成基线：已经讨论过的材料提交',-100)
    source=FixtureMac([baseline]);cache=ChatCache(tmp_path/'chat.sqlite3');cache.activate(source.dataset);cache.receive(source.dataset,[baseline]);cache.process()
    db=V3Database.open(tmp_path/'core.sqlite3');factory=lambda:SqliteUnitOfWork(db)
    mem=CrossDayMemoryService(CrossDayMemorySources(db,factory,cache.path),factory)
    day=datetime.fromtimestamp(now,ZoneInfo('Asia/Singapore')).date().isoformat()
    contact=mem.sources.inventory(day,day,'Asia/Singapore')['contacts'][0]['id'];mem.update(day,day,[contact])
    chat=ChatQueries(tmp_path,source,cache,lambda *a:None,start_worker=False)
    trial=MemoryTrial(chat,mem,clock=lambda:clock[0]);chat.memory_trial=trial
    trial.start([contact]);assert trial.saved()['initial_snapshot_policy'].startswith('verified boundary')
    assert not any(route=='/v1/messages' for route,_,_ in source.calls)
    chat.sync_once(scope_ids=trial.saved()['scope_ids'],max_pages=2,seconds=5);chat.cache.process();trial.checked()
    def push(i,text,offset=1,self=False,revision=1,reply=None):
        r=rec(i,text,offset,self,revision)
        if reply is not None:r["reply"]={"record_id":"rec:fixture-"+str(reply)}
        source.records.append(r);return r
    def poll():
        chat.sync_once(scope_ids=trial.saved()['scope_ids'],max_pages=2,seconds=5);cache.process();trial.checked();clock[0]+=31;trial.tick();return trial.status()
    return db,cache,mem,chat,trial,clock,push,poll,rec

def test_live_history_commit_duplicate_and_reconsider_interest(tmp_path):
    db,cache,mem,chat,trial,clock,push,poll,rec=setup(tmp_path)
    push(2,'合成历史补录：另一条旧讨论',-50)
    push(3,'国庆购机活动到店享优惠',1)
    push(4,'<msg><img aeskey="fictional-attachment-secret" cdnthumburl="https://example.invalid/a?token=fictional"/></msg>',2)
    status=poll();assert status['new_messages']==2 and status['historical_backfill']==1
    assert status['discussion_counts']['processed_discussions']==0
    assert status['discussion_counts']['excluded_discussions']==1
    # Related self inquiry reconsiders the earlier promotion; exclusion is no account label.
    push(5,'这款型号多少钱？我想买一台',3,True,reply=3)
    status=poll();assert status['discussion_counts']['processed_discussions']==1
    assert status['processed_messages']==3 and status['excluded_messages']==0
    with mem.uow_factory().reading() as u: _,full=u.cross_day_index.current()
    assert 'fictional-attachment-secret' not in str(full)
    revision=status['last_memory_revision'];counts=cache.trial_counts(trial.saved()['id'])
    assert poll()['last_memory_revision']==revision
    assert cache.trial_counts(trial.saved()['id'])==counts
    assert trial.saved()['baseline_memory_revision']==1
    assert chat._paused # trial did not resume normal unrelated chat scopes
    with db.read() as c:assert c.execute('select count(*) from reminder_schedules').fetchone()[0]==0

def test_fault_rollback_restart_pause_resume_stop_expire(tmp_path,monkeypatch):
    db,cache,mem,chat,trial,clock,push,poll,rec=setup(tmp_path)
    before=cache.scope(trial.saved()['scope_ids'][0])['receive_cursor']
    chat.source.failure='MAC_OFFLINE';poll()
    assert cache.scope(trial.saved()['scope_ids'][0])['receive_cursor']==before
    assert trial.status()['source_error']=='MAC_OFFLINE'
    chat.source.failure=None;push(2,'合成讨论具体事情编号2',1)
    update=trial.processor.update
    monkeypatch.setattr(trial.processor,'update',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('synthetic failure')))
    failed=poll();assert failed['pending_messages']==1 and failed['failures']==1
    restarted=MemoryTrial(chat,mem,clock=lambda:clock[0]);chat.memory_trial=restarted
    restarted.tick();assert restarted.status()['pending_messages']==0
    assert restarted.status()['discussion_counts']['processed_discussions']==1
    restarted.control('pause');push(3,'合成讨论具体事情编号3',2)
    clock[0]+=31;restarted.tick();assert restarted.status()['new_messages']==1
    # The owned scopes cannot be pulled by normal sync while paused.
    chat._paused=False;chat.sync_once();assert restarted.status()['new_messages']==1
    restarted.control('resume');chat.sync_once(scope_ids=restarted.saved()['scope_ids']);cache.process();clock[0]+=31;restarted.tick()
    assert restarted.status()['new_messages']==2
    cursors=[r['receive_cursor'] for r in cache.scopes()]
    restarted.control('stop');restarted.tick();assert restarted.status()['state']=='stopped'
    assert [r['receive_cursor'] for r in cache.scopes()]==cursors
    assert not any(r['enabled'] for r in cache.scopes())
    # Restart doesn't re-enable a stopped run; expiry works also while paused.
    assert MemoryTrial(chat,mem,clock=lambda:clock[0]).status()['state']=='stopped'
    run=restarted.saved();run['state']='paused';cache.put(TRIAL_KEY,run);clock[0]=run['ends_at']
    assert restarted.status()['state']=='ended'

def test_bounded_fair_arrivals_manual_decisions_revisions_and_atomic_receive(tmp_path):
    db,cache,mem,chat,trial,clock,push,poll,rec=setup(tmp_path)
    for n in range(2,33):push(n,'合成讨论具体事情编号'+str(n),n)
    first=poll();assert first['discussion_counts']['processed_discussions']==7 and first['discussion_counts']['deferred_discussions']==24
    with mem.uow_factory().reading() as u: _,full=u.cross_day_index.current()
    a,b=[r['id'] for r in full['discussions'] if r['id'] in full['_active_ids']][:2]
    value=mem.decide(a,b,'link',full['revision']);value=mem.decide(a,b,'detach',value['revision']);mem.decide(a,b,'reject',value['revision'])
    with db.read() as c: decisions=[tuple(r) for r in c.execute('select * from cross_day_link_decisions')]
    push(33,'合成讨论具体事情编号33，新互动实际收到资料',33)
    second=poll();assert second['discussion_counts']['processed_discussions']==14
    with mem.uow_factory().reading() as u: scope,full=u.cross_day_index.current()
    assert any('编号33' in e['quote'] for r in scope['manifest'] for e in r['evidence'])
    for _ in range(4):poll()
    assert trial.status()['pending_messages']==0
    with db.read() as c: assert decisions==[tuple(r) for r in c.execute('select * from cross_day_link_decisions')]
    sid=trial.saved()['scope_ids'][0];old=cache.scope(sid);count=trial.status()['new_messages']
    with pytest.raises(Exception):
        cache.receive(chat.source.dataset,[{'record_id':'bad'}],scope_id=sid,progress={'receive_cursor':'not-committed'},changes=False)
    assert cache.scope(sid)==old and trial.status()['new_messages']==count
    revised=rec(2,'合成已修订原文，不应覆盖旧人工决定',2,revision=2)
    cache.receive(chat.source.dataset,[{'operation':'update','record':revised,'record_id':revised['record_id'],'record_revision':2}],scope_id=sid,changes=True)
    cache.process();clock[0]+=31;trial.tick()
    assert trial.status()['source_revisions']==1
    with mem.uow_factory().reading() as u: _,full=u.cross_day_index.current()
    assert any(r['evidence_status']!='valid' for r in full['discussions'] if r['id']==a)
    with db.read() as c: assert decisions==[tuple(r) for r in c.execute('select * from cross_day_link_decisions')]
    deleted=rec(3,'合成删除目标',3,revision=2)
    cache.receive(chat.source.dataset,[{'operation':'delete','record':deleted,'record_id':deleted['record_id'],'record_revision':2}],scope_id=sid,changes=True)
    cache.process();clock[0]+=31;trial.tick()
    assert trial.status()['source_revisions']==2
    with mem.uow_factory().reading() as u: _,full=u.cross_day_index.current()
    assert next(r for r in full['discussions'] if r['id']==b)['evidence_status']!='valid'
    with db.read() as c: assert decisions==[tuple(r) for r in c.execute('select * from cross_day_link_decisions')]


def test_worker_is_trigger_without_any_page_request(tmp_path,monkeypatch):
    db,cache,mem,chat,trial,clock,push,poll,rec=setup(tmp_path)
    push(2,'合成讨论具体事情编号2：源端正常新消息',1)
    clock[0]+=5
    # Exercise the production worker loop itself, stop after receipt publication.
    original=trial.tick
    def tick():
        original()
        if cache.trial_counts(trial.saved()['id'])['processed_messages']:chat._stop.set()
    monkeypatch.setattr(trial,'tick',tick)
    chat._worker.start();chat._worker.join(timeout=8)
    assert not chat._worker.is_alive()
    assert trial.status()['processed_messages']==1
    assert trial.status()['model_calls']==0


def test_commit_publication_interruption_and_missing_commit_time(tmp_path,monkeypatch):
    db,cache,mem,chat,trial,clock,push,poll,rec=setup(tmp_path)
    push(2,'合成讨论具体事情编号2，等待发布',1)
    finish=cache.finish_trial_tasks
    monkeypatch.setattr(cache,'finish_trial_tasks',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('stop after core commit')))
    failed=poll();assert failed['pending_messages']==1 and trial.saved()['inflight_batch']
    with mem.uow_factory().reading() as u: _,published=u.cross_day_index.current()
    monkeypatch.setattr(cache,'finish_trial_tasks',finish)
    restarted=MemoryTrial(chat,mem,clock=lambda:clock[0]);restarted.tick()
    assert restarted.status()['pending_messages']==0
    with mem.uow_factory().reading() as u: _,recovered=u.cross_day_index.current()
    assert recovered['revision']==published['revision']
    assert not restarted.saved().get('inflight_batch')
    # An interruption after receive commit cannot invent the missing commit time.
    with cache.connect() as c:c.execute('UPDATE tasks SET memory_committed_at=NULL WHERE memory_trial=?',(trial.saved()['id'],))
    receipt=restarted.status()['recent_receipts'][0]
    assert receipt['memory_committed_at'] is None and receipt['source_to_cache_seconds'] is None
    assert receipt['phone_successful_sync_at'] is None
    assert receipt['commit_timestamp_status']=='unknown_after_interruption'


def test_original_sync_scope_restore_and_cursor_proof(tmp_path):
    db,cache,mem,chat,trial,clock,push,poll,rec=setup(tmp_path)
    ids=trial.saved()['scope_ids'];assert trial.poll_scopes()==ids
    # Even a future sent_at cannot create a live receipt without committed changes cursors.
    raw=rec(2,'合成缺少游标的来源记录',1)
    cache.receive(chat.source.dataset,[{'operation':'insert','record':raw,'record_id':raw['record_id'],'record_revision':1}],scope_id=ids[0],changes=True)
    assert trial.status()['new_messages']==0 and trial.status()['historical_backfill']==1
    # Existing normal enabled scope is restored, not disabled by stopping a trial.
    run=trial.saved();run['prior_enabled'][ids[0]]=True;cache.put(TRIAL_KEY,run)
    trial.control('stop');assert cache.scope(ids[0])['enabled']==1


def test_poll_scope_rotation_persistent_without_cursor_advancement(tmp_path):
    db,cache,mem,chat,trial,clock,push,poll,rec=setup(tmp_path)
    run=trial.saved();run['scope_ids']=['fictional-first','fictional-second'];run['prior_enabled']={i:False for i in run['scope_ids']};cache.put(TRIAL_KEY,run)
    assert trial.poll_scopes()==['fictional-first','fictional-second']
    restarted=MemoryTrial(chat,mem,clock=lambda:clock[0])
    assert restarted.poll_scopes()==['fictional-second','fictional-first']
    assert restarted.saved()['baseline_cursors']==run['baseline_cursors']


def test_baseline_revision_invalidates_saved_memory_without_history_reanalysis(tmp_path):
    db,cache,mem,chat,trial,clock,push,poll,rec=setup(tmp_path)
    before=mem.get();baseline=next(r for r in before['discussions'] if any(e.get('record_id')=='rec:fixture-1' for e in r['evidence']))
    revised=rec(1,'合成基线原文修订',-100,revision=2);sid=trial.saved()['scope_ids'][0]
    cache.receive(chat.source.dataset,[{'operation':'update','record':revised,'record_id':revised['record_id'],'record_revision':2}],scope_id=sid,changes=True)
    cache.process();clock[0]+=31;trial.tick()
    status=trial.status();assert status['new_messages']==0 and status['source_revisions']==1 and status['pending_messages']==0
    with mem.uow_factory().reading() as u: scope,value=u.cross_day_index.current()
    assert next(r for r in value['discussions'] if r['id']==baseline['id'])['evidence_status']!='valid'
    assert len(value['discussions'])==1 and scope['first']==scope['last']


def test_failed_index_does_not_starve_new_ready_interaction(tmp_path):
    db,cache,mem,chat,trial,clock,push,poll,rec=setup(tmp_path)
    push(2,'合成讨论具体事情编号2',1);chat.sync_once(scope_ids=trial.saved()['scope_ids'])
    cache.process(fail=lambda task:(_ for _ in ()).throw(RuntimeError('fictional index failure')))
    clock[0]+=31;trial.tick();assert trial.status()['failed_index_messages']==1
    push(3,'合成讨论具体事情编号3，新的有价值互动',2)
    poll();assert trial.status()['processed_messages']==1 and trial.status()['failed_index_messages']==1
    cache.retry_index();cache.process();clock[0]+=31;trial.tick()
    assert trial.status()['processed_messages']==2 and trial.status()['failed_index_messages']==0
