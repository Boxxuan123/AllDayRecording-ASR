"""Only committed trial receipts are analysis input; startup history is not replayed."""
import json
from .cross_day_memory_sources import CrossDayMemorySources
from .memory_trial_cache import TRIAL_KEY
from allday_asr.v3.ports.chat_data import ChatDataError

class MemoryTrialSources(CrossDayMemorySources):
    def __init__(self, database, factory, cache):
        super().__init__(database,factory,cache.path)
        self.cache=cache

    def _read(self, first, last, timezone):
        trial=self.cache.get(TRIAL_KEY)
        if not trial or [first,last,timezone]!=[trial['first'],trial['last'],trial['timezone']]: raise ValueError('trial scope mismatch')
        with self.cache.connect() as c:
            selected=c.execute("SELECT DISTINCT r.dataset,r.body FROM tasks t JOIN search_index r ON r.dataset=t.dataset AND r.id=t.record_id WHERE t.memory_trial=? AND t.memory_eligible=1 ORDER BY r.sent_at,r.id LIMIT 5001",(trial['id'],)).fetchall()
            if len(selected)>5000: raise ChatDataError('TRIAL_RECORD_BUDGET_REACHED')
            records=[{**json.loads(r['body']),'dataset':r['dataset']} for r in selected]
        from .source_identity_repository import SourceIdentities
        from allday_asr.v3.domain.source_identity import effective_self
        resolver=SourceIdentities(self.database,self.chat_path)
        people,manual=resolver.people(),resolver.manual()
        for record in records:record['effective_is_self']=effective_self(record,manual,people)
        contacts={p['id']:p for p in trial['contacts']}
        conversations={}
        for p,scope in zip(trial['contacts'],trial['scopes']):
            conversations[(trial['dataset'],p['platform'],p['source_account_id'],scope['conversation_id'])]={p['id']}
        # Reuse existing followup identifiers only for the selected message references.
        followups={}
        with self.database.read() as c:
            refs=json.dumps([{'dataset':r['dataset'],'id':r['record_id']} for r in records])
            query="SELECT e.event_id,json_extract(f.value,'$.dataset') AS dataset,json_extract(f.value,'$.id') AS rid FROM event_current_states e JOIN json_each(?) f ON json_extract(e.payload_json,'$.chat_followup.dataset')=json_extract(f.value,'$.dataset') WHERE EXISTS(SELECT 1 FROM json_each(json_extract(e.payload_json,'$.chat_followup.evidence')) WHERE json_extract(value,'$.record_id')=json_extract(f.value,'$.id'))"
            for e in c.execute(query,(refs,)): followups[(e['dataset'],e['rid'])]=e['event_id']
        coverage={'chat_cache_available':True,'record_counts':{'trial':len(records)},'recording_events':0,'missing_sources':[],'input_selection':'committed trial receipts only'}
        return records,contacts,conversations,[],followups,coverage
