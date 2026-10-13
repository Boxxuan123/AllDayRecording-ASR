"""Three-day bounded automation inside the existing chat worker; models disabled."""
import time
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from threading import RLock
from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.chat_data import scope_key
from allday_asr.v3.adapters.sqlite.memory_trial_cache import TRIAL_KEY
from allday_asr.v3.adapters.sqlite.memory_trial_sources import MemoryTrialSources
from .cross_day_memory import CrossDayMemoryService

class MemoryTrial:
    def __init__(self, chat, memory, *, clock=time.time):
        self.chat,self.cache,self.memory,self.clock=chat,chat.cache,memory,clock
        self.lock=RLock()
        self.processor=CrossDayMemoryService(MemoryTrialSources(memory.sources.database,memory.uow_factory,self.cache),memory.uow_factory)
        self.next_process=0
        self.ensure_source_baseline()

    def saved(self): return self.cache.get(TRIAL_KEY)

    def ensure_source_baseline(self):
        run=self.saved()
        if not run or run['state'] not in {'running','paused'}: return
        # The source snapshot boundary is the baseline, not a history import job.
        # Only trial-owned, untouched snapshots may begin at this verified boundary.
        changed=False
        with self.cache.connect() as c:
            for sid in run['scope_ids']:
                if run['prior_enabled'][sid]: continue
                baseline=run['baseline_cursors'].get(sid,{})
                row=c.execute('SELECT * FROM scopes WHERE id=?',(sid,)).fetchone()
                if row and row['phase']=='snapshot' and row['received']==0 and row['boundary']==baseline.get('boundary') and row['boundary']:
                    c.execute("UPDATE scopes SET phase='changes',receive_cursor=boundary,page_cursor=NULL,error=NULL WHERE id=?",(sid,));changed=True
            if changed:
                run['initial_snapshot_policy']='verified boundary only; no startup historical import'
                self.cache._put(c,TRIAL_KEY,run)

    def poll_scopes(self):
        with self.lock:
            run=self.saved()
            if not run or run['state']!='running': return []
            ids=run['scope_ids'];offset=run.get('poll_offset',0)%len(ids)
            run['poll_offset']=(offset+1)%len(ids);self.cache.put(TRIAL_KEY,run)
            return ids[offset:]+ids[:offset]

    def choices(self):
        with self.memory.uow_factory().reading() as u:
            scope,_=u.cross_day_index.current()
        return [p for p in (scope or {}).get('contacts',[]) if p['platform'] in {'qq','wechat'}]

    def start(self, contacts):
        with self.lock,self.chat._sync_lock:
            old=self.saved()
            if old and old['state'] in {'running','paused'}: raise ValueError('trial already exists; resume or stop it')
            if not isinstance(contacts,list) or not all(isinstance(i,str) for i in contacts): raise ValueError('select existing contact identifiers')
            selected=[p for p in self.choices() if p['id'] in contacts]
            if not isinstance(contacts,list) or not 1<=len(contacts)<=3 or len(selected)!=len(set(contacts)) or len(set(contacts))!=len(contacts): raise ValueError('select one to three existing contacts')
            if len({(p['platform'],p['source_account_id']) for p in selected})!=1: raise ValueError('trial requires one source account')
            now=self.clock();day=datetime.fromtimestamp(now,ZoneInfo('Asia/Singapore')).date()
            end=datetime.combine(day+timedelta(days=3),datetime.min.time(),ZoneInfo('Asia/Singapore')).timestamp()
            scopes=[];baseline_records=0
            with self.cache.connect() as c:
                for p in selected:
                    rows=c.execute("SELECT DISTINCT conversation FROM search_index WHERE platform=? AND source=? AND sender=? AND json_extract(body,'$.conversation_type')='c2c' LIMIT 2",(p['platform'],p['source_account_id'],p['account_id'])).fetchall()
                    if len(rows)!=1: raise ValueError('existing direct conversation must be unique')
                    scopes.append({'platform':p['platform'],'source_account_id':p['source_account_id'],'conversation_id':rows[0][0],'sent_from':int(datetime.combine(day,datetime.min.time(),ZoneInfo('Asia/Singapore')).timestamp()),'sent_to':int(end)-1})
                    baseline_records+=c.execute("SELECT count(*) FROM search_index WHERE platform=? AND source=? AND conversation=?",(p['platform'],p['source_account_id'],rows[0][0])).fetchone()[0]
                baseline_task=c.execute('SELECT coalesce(max(id),0) FROM tasks').fetchone()[0]
            before={r['id']:r['enabled'] for r in self.cache.scopes()};was_paused=self.chat._paused
            try:
                result=self.chat.start_sync(scopes,trial_mode=True)
                dataset=self.cache.get('dataset');ids=[scope_key({'dataset':dataset,'scope':s}) for s in scopes]
                with self.memory.uow_factory().reading() as u: _,value=u.cross_day_index.current()
                run={'id':new_ulid(),'state':'running','started_at':now,'ends_at':end,'first':day.isoformat(),'last':(day+timedelta(days=2)).isoformat(),'timezone':'Asia/Singapore','dataset':dataset,'contacts':selected,'scopes':scopes,'scope_ids':ids,'prior_enabled':{i:before.get(i,False) for i in ids},'baseline_cursors':{r['id']:{k:r[k] for k in ['phase','boundary','receive_cursor','page_cursor']} for r in result['scopes'] if r['id'] in ids},'baseline_task':baseline_task,'baseline_record_policy':'stable pre-existing records/revisions plus snapshot boundary; snapshot deliveries are conservative backfill','baseline_records':baseline_records,'baseline_memory_revision':value['revision'] if value else 0,'last_memory_revision':value['revision'] if value else 0,'model_calls':0,'model_daily_limit':2,'model_total_limit':6,'model_policy':'disabled; deterministic rules only','failures':0,'error':None,'last_check_at':None,'latest_message_at':None,'last_completed_at':None,'discussion_counts':{'processed_discussions':0,'deferred_discussions':0,'excluded_discussions':0}}
                self.cache.put(TRIAL_KEY,run)
                self.ensure_source_baseline()
            except Exception:
                for row in self.cache.scopes():
                    if row['id'] not in before: self.cache.enable_scope(row['id'],False)
                raise
            finally: self.chat._paused=was_paused
            return self.status()

    def control(self,action):
        with self.lock,self.chat._sync_lock:
            run=self.saved()
            if not run: raise ValueError('trial not started')
            if action=='pause' and run['state']=='running': run['state']='paused'
            elif action=='resume' and run['state']=='paused' and self.clock()<run['ends_at']: run['state']='running';run['error']=None
            elif action=='stop':
                run['state']='stopped';run['stopped_at']=self.clock()
                for i in run['scope_ids']: self.cache.enable_scope(i,run['prior_enabled'][i])
            else: raise ValueError('invalid trial transition')
            self.cache.put(TRIAL_KEY,run)
            return self.status()

    def expire(self):
        with self.lock:
            run=self.saved()
            if run and run['state'] in {'running','paused'} and self.clock()>=run['ends_at']:
                run['state']='ended';run['stopped_at']=self.clock()
                for i in run['scope_ids']: self.cache.enable_scope(i,run['prior_enabled'][i])
                self.cache.put(TRIAL_KEY,run)
            return run

    def checked(self):
        with self.lock:
            run=self.saved()
            if not run or run['state']!='running': return
            rows=[r for r in self.cache.scopes() if r['id'] in run['scope_ids']]
            run['last_check_at']=self.clock();run['source_error']=next((r['error'] for r in rows if r['error']),None)
            if run['source_error']: run['source_failed_checks']=run.get('source_failed_checks',0)+1
            run['current_cursors']={r['id']:{k:r[k] for k in ['phase','receive_cursor','page_cursor']} for r in rows}
            self.cache.put(TRIAL_KEY,run)

    def tick(self):
        with self.lock:
            run=self.expire()
            if not run or run['state']!='running' or self.clock()<self.next_process: return
            tasks=self.cache.trial_tasks(run['id'],pending=True,ready=True,limit=100)
            if not tasks:
                if self.cache.trial_counts(run['id'])['failed_index_messages']:
                    run['error']='CHAT_INDEX_FAILED';self.cache.put(TRIAL_KEY,run)
                completed=self.cache.trial_tasks(run['id'])
                completed=[t for t in completed if t['memory_completed_at'] is not None]
                if run.get('inflight_batch'):
                    run.pop('inflight_batch',None);self.cache.put(TRIAL_KEY,run)
                if completed:
                    revision=max(t['memory_revision'] for t in completed)
                    if revision>run['last_memory_revision']:
                        run['last_memory_revision']=revision;run['last_completed_at']=max(t['memory_completed_at'] for t in completed)
                        self.cache.put(TRIAL_KEY,run)
                return
            if self.clock()<max(t['received_at'] for t in tasks)+2: return
            self.next_process=self.clock()+30
            try:
                batch='trial-'+run['id']+'-'+str(tasks[0]['id'])+'-'+str(tasks[-1]['id'])
                # A retried partially admitted batch must drain existing saved backlog
                # through new stable IDs; publication and receipt marking are separately durable.
                with self.memory.uow_factory().reading() as u: scope,value=u.cross_day_index.current()
                count=len((scope or {}).get('manifest',[]));batch+='-'+str(count)
                if run.get('inflight_batch'):
                    batch=run['inflight_batch']['id']
                    tasks=[t for t in tasks if t['id']<=run['inflight_batch']['last_id']]
                else:
                    run['inflight_batch']={'id':batch,'last_id':tasks[-1]['id']}
                    self.cache.put(TRIAL_KEY,run)
                revisions=[t for t in tasks if t['memory_class']=='revision']
                if revisions:
                    self.memory.refresh_saved()  # Existing frozen citations only, no history extraction.
                    with self.memory.uow_factory().reading() as u: _,saved=u.cross_day_index.current()
                    refs={(t['dataset'],t['record_id']) for t in revisions}
                    affected=[r['id'] for r in saved['discussions'] if any((e.get('dataset'),e.get('record_id')) in refs for e in r['evidence'])]
                    for ident in affected: self.memory.refresh_targets(ident,ident)
                if any(t['memory_eligible']==1 for t in tasks):
                    self.processor.update(run['first'],run['last'],[p['id'] for p in run['contacts']],continue_pending=True,batch_id=batch)
                with self.memory.uow_factory().reading() as u: scope,full=u.cross_day_index.current()
                admitted={(e.get('dataset'),e.get('record_id')) for r in scope['manifest'] for e in r['evidence']}
                excluded={(e['dataset'],e['record_id']) for r in scope.get('_excluded',[]) for e in r['source_refs']}
                waiting={(e.get('dataset'),e.get('record_id')) for r in scope.get('_pending',[]) for e in r['evidence']}
                finishes=[]
                for t in self.cache.trial_tasks(run['id']):
                    if not t['memory_eligible'] or t['state']!='done': continue
                    key=(t['dataset'],t['record_id'])
                    if t['memory_eligible']==2: finishes.append((t['id'],'processed'))
                    elif key in admitted: finishes.append((t['id'],'processed'))
                    elif key in excluded or key not in waiting: finishes.append((t['id'],'excluded'))
                self.cache.finish_trial_tasks(run['id'],finishes,full['revision'])
                run.pop('inflight_batch',None)
                run['last_memory_revision']=full['revision'];run['last_completed_at']=self.clock();run['discussion_counts']=scope['coverage']['incremental'] if (scope['first'],scope['last'])==(run['first'],run['last']) else run['discussion_counts'];run['error']=None
                self.cache.put(TRIAL_KEY,run)
            except Exception as exc:
                run['failures']+=1;run['error']=getattr(exc,'code','MEMORY_BATCH_FAILED')
                if run['error']=='TRIAL_RECORD_BUDGET_REACHED': run['state']='paused'
                self.cache.put(TRIAL_KEY,run)

    def worker_failed(self):
        with self.lock:
            run=self.saved()
            if run and run['state']=='running':
                run['error']='CHAT_WORKER_FAILED';run['failures']+=1;self.cache.put(TRIAL_KEY,run)

    def status(self):
        run=self.expire()
        if not run: return {'state':'not_started','choices':self.choices(),'model_calls':0}
        result={**run,**self.cache.trial_counts(run['id']),'choices':self.choices()}
        with self.cache.connect() as c:
            latest=c.execute("SELECT max(json_extract(r.body,'$.sent_at')) FROM tasks t JOIN records r ON r.dataset=t.dataset AND r.id=t.record_id WHERE t.memory_trial=? AND t.memory_class='live'",(run['id'],)).fetchone()[0]
            times=[dict(r) for r in c.execute("SELECT t.record_id,t.revision,t.memory_class,t.received_at,t.memory_committed_at,t.memory_completed_at,t.memory_revision,json_extract(r.body,'$.sent_at') AS message_at FROM tasks t JOIN records r ON r.dataset=t.dataset AND r.id=t.record_id WHERE t.memory_trial=? ORDER BY t.id DESC LIMIT 20",(run['id'],))]
        result['latest_message_at']=latest;result['recent_receipts']=times
        with self.memory.sources.database.read() as c:
            from allday_asr.v3.domain.cross_day_memory import INDEX_ID
            sequence=c.execute("SELECT max(sequence) FROM change_events WHERE resource_type='memory_index' AND resource_id=? AND revision=?",(INDEX_ID,run['last_memory_revision'])).fetchone()[0]
            result['memory_sequence']=sequence
            result['phone_sync']=[{**dict(r),'acknowledged_current_memory':sequence is not None and r['acknowledged_sequence']>=sequence} for r in c.execute('SELECT acknowledged_sequence,updated_at FROM sync_cursors ORDER BY updated_at DESC LIMIT 3')]
            receipt_sequences={r['memory_revision']:c.execute("SELECT max(sequence) FROM change_events WHERE resource_type='memory_index' AND resource_id=? AND revision=?",(INDEX_ID,r['memory_revision'])).fetchone()[0] for r in times}
        for receipt in times:
            target=receipt_sequences.get(receipt['memory_revision'])
            acknowledged=[r['updated_at'] for r in result['phone_sync'] if target is not None and r['acknowledged_sequence']>=target]
            receipt['phone_successful_sync_at']=min(acknowledged) if acknowledged else None
            receipt['phone_confirmation_semantics']='retained acknowledgement covering revision; first acknowledgement time not retained'
            receipt['commit_timestamp_status']='recorded_after_successful_commit' if receipt['memory_committed_at'] is not None else 'unknown_after_interruption'
            delta=receipt['memory_committed_at']-receipt['message_at'] if receipt['message_at'] is not None and receipt['memory_committed_at'] is not None else None
            receipt['source_to_cache_seconds']=delta if delta is not None and delta>=0 else None
            receipt['source_clock_ahead_seconds']=-delta if delta is not None and delta<0 else None
            receipt['cache_to_memory_seconds']=receipt['memory_completed_at']-receipt['memory_committed_at'] if receipt['memory_completed_at'] is not None and receipt['memory_committed_at'] is not None else None
        # Opaque source cursors stay private local status, never logs or Phone projections.
        return result
