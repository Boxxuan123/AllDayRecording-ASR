"""Fixed synthetic boundaries, bounded fair backlog and historical decisions: model zero."""
from copy import deepcopy
import pytest
from tests.test_cross_day_memory_incremental import setup
from tests.test_cross_day_memory_v1 import Sources, NOW
from allday_asr.v3.application.cross_day_memory import CrossDayMemoryService
from allday_asr.v3.adapters.sqlite import V3Database,SqliteUnitOfWork
from allday_asr.v3.adapters.sqlite.cross_day_index_repository import memory_review_items
from allday_asr.v3.application.mobile_sync import MobileSyncService,UtteranceCorrectionOperationHandler
from allday_asr.v3.domain.device_sync import ClientOperation,SyncRequest,PROJECTION_VERSION
from allday_asr.v3.domain.ids import stable_ulid,new_ulid

def test_exact_7_24_new_fair_restart_idempotent_and_no_eviction(tmp_path):
    db,cache,sources,service,arrive,update=setup(tmp_path)
    for n in range(31): arrive(n+1,'讨论具体事情编号'+str(n+1),n)
    first=update(); ids={r['id'] for r in first['discussions']}
    assert len(ids)==7
    assert first['scope']['coverage']['incremental']['deferred_discussions']==24
    a,b=list(ids)[:2]
    linked=service.decide(a,b,'link',first['revision'])
    contacts=[p['id'] for p in linked['scope']['contacts']]
    arrive(32,'讨论具体事情编号32，新互动已经确认收到资料',32)
    second=update()
    with db.read() as c:
        scope,value=__import__('allday_asr.v3.adapters.sqlite.cross_day_index_repository',fromlist=['CrossDayIndexRepository']).CrossDayIndexRepository(c).current()
    assert len(value['discussions'])==14 and len(scope['_pending'])==18
    assert any('32' in r['evidence'][0]['quote'] for r in value['discussions'])
    assert ids<={r['id'] for r in value['discussions']}
    assert next(r for r in value['discussions'] if r['id']==a)['thing_id']==next(r for r in value['discussions'] if r['id']==b)['thing_id']
    restarted=CrossDayMemoryService(sources,lambda:SqliteUnitOfWork(db),now=lambda:NOW)
    assert restarted.get()==second
    for n in range(3):
        value=restarted.update('2026-10-05','2026-10-07',contacts,continue_pending=True,batch_id='batch-'+str(n))
        assert restarted.update('2026-10-05','2026-10-07',contacts,continue_pending=True,batch_id='batch-'+str(n))==value
    assert value['scope']['coverage']['incremental']['processed_discussions']==32
    assert value['scope']['coverage']['incremental']['deferred_discussions']==0
    assert len(service.get(page=4)['discussions'])==4
    before=value['revision']
    assert update()['revision']==before
    arrive(33,'国庆购机活动到店享优惠',33)
    excluded=update()
    assert excluded['scope']['coverage']['incremental']['processed_discussions']==32
    assert excluded['scope']['coverage']['incremental']['excluded_discussions']==1
    with db.read() as c:
        assert c.execute('select count(*) from reminder_schedules').fetchone()[0]==0

def test_object_boundaries_not_batch_size_core_metadata_and_evidence_pages(tmp_path):
    db,cache,sources,service,arrive,update=setup(tmp_path)
    for n,text in enumerate(['维修单 MP-1 的照明亮度不足','维修单 MP-1 调整灯位','维修单 MP-1 还需检查接线','维修单 MP-1 暂未完成','申请单 MP-2 专业选择尚未确定','申请单 MP-2 报名专业待核对']):
        arrive(n+1,text,n,self=n in {2,3,5})
    value=update()
    assert len(value['discussions'])==2
    lighting,application=value['discussions']
    assert all('专业' not in e['quote'] and '报名专业' not in e['quote'] for e in lighting['evidence'] if e['role']=='core')
    assert any('报名专业' in e['quote'] for e in application['evidence'])
    for r in value['discussions']:
        assert all(type(e['is_from_self']) is bool and e['speaker'] and e['identity_basis'] for e in r['evidence'])
    for n in range(7,18): arrive(n,'维修单 MP-1 细节说明'+str(n),n)
    value=update()
    row=max(value['discussions'],key=lambda r:r['evidence_total'])
    assert row['evidence_total']>8 and len(row['evidence'])==8
    more=service.get(discussion=row['id'],evidence_page=1)
    assert more['discussions'][0]['evidence']

class WindowSources(Sources):
    def freeze(self,first,last,*args):
        scope=deepcopy(self.scope);scope.update(first=first,last=last)
        scope['manifest']=deepcopy(self.rows[:2] if first=='2026-10-05' else self.rows[2:])
        return scope
    def load(self,scope):
        ids={r['id'] for r in scope['manifest']}
        return deepcopy([r for r in self.rows if r['id'] in ids])

def test_old_candidate_after_window_switch_mobile_receipts_replay_and_invalid_source(tmp_path):
    db=V3Database.open(tmp_path/'core.sqlite3');factory=lambda:SqliteUnitOfWork(db)
    sources=WindowSources()
    for row in sources.rows: row['event_id']=None
    sources.rows[1]['evidence'][0]['reply_record_id']='0'
    service=CrossDayMemoryService(sources,factory,now=lambda:NOW)
    contact=sources.scope['contacts'][0]['id']
    old=service.update('2026-10-05','2026-10-07',[contact]);candidate=old['candidates'][0]
    current=service.update('2026-10-08','2026-10-10',[contact])
    with db.read() as c:
        reviews=memory_review_items(c)
        assert any(r['source_id']==candidate['source'] for r in reviews)
    device=stable_ulid('historical-fictional-phone')
    with db.transaction() as c:c.execute("INSERT INTO devices VALUES(?,'phone','FICTIONAL','active',1,NULL,NULL,NULL,?,?)",(device,NOW.isoformat(),NOW.isoformat()))
    sync=MobileSyncService(factory,operation_handler=UtteranceCorrectionOperationHandler(now=lambda:NOW),now=lambda:NOW)
    sync.memory_refresh=service.refresh_saved;sync.memory_target_refresh=service.refresh_targets
    for action in ['reject','link','detach']:
        current=service.get();op=ClientOperation(new_ulid(),'memory.link',current['revision'],{**{k:candidate[k] for k in ['source','target']},'action':action})
        request=SyncRequest(PROJECTION_VERSION,None,(op,),500)
        response=sync.synchronize(device,request)
        assert response.receipts[0].status.value=='applied'
        assert sync.synchronize(device,request).receipts==response.receipts
    assert not any(c['source']==candidate['source'] and c['target']==candidate['target'] for c in service.get()['candidates'])
    before=service.get()
    assert CrossDayMemoryService(sources,factory,now=lambda:NOW).get()==before
    stale=ClientOperation(new_ulid(),'memory.link',old['revision'],{k:candidate[k] for k in ['source','target']}|{'action':'link'})
    assert sync.synchronize(device,SyncRequest(PROJECTION_VERSION,None,(stale,),500)).receipts[0].status.value=='conflict'
    sources.rows[0]['evidence_status']='source_changed_or_missing'
    sources.rows[0]['evidence'][0]['status']='source_changed_or_missing'
    service.refresh_targets(candidate['source'],candidate['target'])
    assert any(r['evidence_status']!='valid' for r in service.get(page=1)['discussions'])


def test_failed_batch_does_not_consume_persistent_progress(tmp_path,monkeypatch):
    db,cache,sources,service,arrive,update=setup(tmp_path)
    for n in range(12):arrive(n+1,'讨论具体事情编号'+str(n+1),n)
    before=update();contact=before['scope']['contacts'][0]['id']
    load=sources.load;counter=0
    def unstable(scope):
        nonlocal counter
        counter+=1
        result=load(scope)
        if counter==2: result[0]['pending_change']=True
        return result
    monkeypatch.setattr(sources,'load',unstable)
    with pytest.raises(ValueError):service.update('2026-10-05','2026-10-07',[contact],continue_pending=True,batch_id='failed-batch')
    monkeypatch.setattr(sources,'load',load)
    assert service.get()==before
    after=service.update('2026-10-05','2026-10-07',[contact],continue_pending=True,batch_id='failed-batch')
    assert after['scope']['coverage']['incremental']['processed_discussions']==12
