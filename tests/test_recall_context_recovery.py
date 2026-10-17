"""A slow context locator may use one bounded native-room read, never guess IDs."""
from copy import deepcopy
import pytest
from tests.test_agentic_review_fixes import remote_tools,raw,SCOPE
from allday_asr.v3.ports.chat_data import ChatDataError

def exercise(*,error='MAC_OFFLINE',changes=None,left=None):
    rows=[raw(record_id='anchor',record_revision=1,text='明天归还设备'),raw(record_id='question',record_revision=1,sender_account_id='owner-self',is_from_self=True,sent_at=990,text='什么时候归还？')]
    if changes:rows=changes(rows)
    tools,_=remote_tools([]);tools.deadline=__import__('time').monotonic()+90
    tools.service.sources.context=lambda *a:[];marked=[];tools.service.sources.mark_changed=lambda *a:marked.append(a)
    original_evidence=tools.service.sources.evidence
    tools.service.sources.evidence=lambda r,origin,role='match':{**original_evidence(r,origin),'role':role}
    calls=[]
    def request(route,body=None,**kw):
        calls.append((route,body,kw))
        if route=='/v1/context':raise ChatDataError(error)
        assert route=='/v1/messages'
        return {'items':deepcopy(rows),'has_more':False}
    tools.service.source.request=request
    if left is not None:tools.result['metrics']['source_requests']=18-left
    target=tools.service.sources.evidence(raw(record_id='anchor',record_revision=1,text='明天归还设备'),'mac');target['type']='evidence'
    refs=tools.context([target],deepcopy(SCOPE))
    return tools,calls,marked,refs

def test_context_timeout_fallback_has_bilateral_content_and_exact_hard_scope():
    tools,calls,marked,refs=exercise()
    assert [c[0] for c in calls]==['/v1/context','/v1/messages']
    scope=calls[1][2]['scope']
    assert scope==dict(platform='qq',source_account_id='owner-qq',conversation_id='chosen-c2c',sent_from=500,sent_to=2000)
    assert {tools.refs[r]['record_id'] for r in refs}=={'anchor','question'}
    assert tools.result['metrics']['source_requests']==2
    assert tools.result['metrics']['body_requests']==2
    assert tools.result['metrics']['source_records_read']==2 and not marked

@pytest.mark.parametrize('error',['AUTHORIZATION_REVOKED','SOURCE_CHANGED','INVALID_CHAT_CONTEXT'])
def test_integrity_or_authorization_errors_do_not_trigger_alternate_reads(error):
    tools,calls,marked,refs=exercise(error=error)
    assert len(calls)==1 and not refs

def test_fallback_rejects_different_native_namespace():
    tools,calls,marked,refs=exercise(changes=lambda rows:[rows[0],dict(rows[1],source_account_id='other-owner')])
    assert not refs
    assert any('CONTEXT_IDENTITY_MISMATCH' in c for c in tools.result['coverage'])

def test_truncated_fallback_without_anchor_does_not_invalidate_an_original_record():
    tools,calls,marked,refs=exercise(changes=lambda rows:rows[1:])
    assert not refs and not marked
    assert any('CONTEXT_ANCHOR_NOT_IN_BOUNDED_PAGE' in c for c in tools.result['coverage'])

def test_new_revision_in_fallback_still_invalidates_old_evidence():
    tools,calls,marked,refs=exercise(changes=lambda rows:[dict(rows[0],record_revision=2),rows[1]])
    assert not refs and marked

def test_fallback_preserves_two_source_slots_for_final_answer_and_original_view():
    tools,calls,marked,refs=exercise(left=3)
    assert len(calls)==1 and not refs
    assert tools.result['metrics']['source_requests']==16

def test_six_remote_slots_are_not_exhausted_just_because_cache_is_exhausted():
    from allday_asr.v3.application.recall_investigation_input import finalization_reason
    left=dict(seconds=74.75,source_requests=6,body_requests=7,cache_records=0)
    assert finalization_reason(1,left)==''
    assert finalization_reason(4,left)=='model_limit'
    assert finalization_reason(1,{**left,'source_requests':2})=='source_budget'
    assert finalization_reason(1,{**left,'body_requests':1})=='source_budget'
    assert finalization_reason(1,{**left,'seconds':29})=='wall_clock'

def test_slow_context_production_entry_still_packs_continuous_unquoted_replies(tmp_path,monkeypatch):
    from tests.recall_scenario_driver import ScenarioSource,run_scenario,contact,message,selected_ids
    original=ScenarioSource.request
    def slow_context(self,route,body=None,**kw):
        if route=='/v1/context':
            self.calls.append((route,deepcopy(body),deepcopy(kw)))
            raise ChatDataError('MAC_OFFLINE')
        return original(self,route,body,**kw)
    monkeypatch.setattr(ScenarioSource,'request',slow_context)
    s=dict(question='昨天群里“用完要归还设备”是怎么回事？',contacts=[contact('qq','equipment','器材协调组',kind='group')],records=[message(80,'有人能借我相机吗','reader','equipment',minute=0),message(81,'用完要还到器材室','keeper','equipment',minute=1),message(82,'用完要归还设备','reader','equipment',minute=2),message(83,'开门时间还没有确认','keeper','equipment',minute=3)],protocol_citation_probe=True)
    run=run_scenario(tmp_path,s)
    assert {'rec:fixture-80','rec:fixture-81','rec:fixture-82','rec:fixture-83'} <= selected_ids(run)
    assert run['stage_ids']['source_returned']
    assert any(route=='/v1/context' for route,_,_ in run['source_queries'])
    assert any(route=='/v1/messages' and params.get('scope',{}).get('conversation_id')=='equipment' for route,_,params in run['source_queries'])
    assert run['result']['metrics']['source_requests']<=18
    assert run['result']['metrics']['body_requests']<=12
    assert all(len(__import__('json').dumps(w,ensure_ascii=False).encode())<=60000 for w in run['wire'])
