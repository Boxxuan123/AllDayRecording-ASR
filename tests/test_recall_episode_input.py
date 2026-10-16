"""Generic episode-block, time and exact outbound-request regressions."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import pytest
from allday_asr.v3.domain.recall_evidence_selection import select_evidence
from allday_asr.v3.domain.recall_input_validation import prepare_payload
from allday_asr.v3.domain.recall_event_time import source_relative_period
from allday_asr.v3.domain.recall_investigation import checked
from allday_asr.v3.application.recall_investigation import _claims
from allday_asr.v3.adapters.codex.model_execution_runner import ModelExecutionRunner, ModelExecutionTerminal
from tests.test_agentic_recall import response, claim
from tests.test_agentic_review_fixes import remote_tools, raw, SCOPE


def row(i,t,room="new",sender="peer",text=None):
    return dict(id=f"chat:{i}",record_id=str(i),revision=1,dataset="D",source="qq",
        source_account_id="owner",conversation_id=room,sender_account_id=sender,
        quote=text if text is not None else f"虚构连续讨论 {i}",sent_at=t)


def test_old_discovery_many_speakers_cannot_starve_new_middle_context():
    history=[row(i,100+i,"old",str(i),"历史发言") for i in range(90)]
    episode=[row(100+i,1000+i,text=t) for i,t in enumerate([
        "几点去机场？","车票尚未确定","先选两点","不，这个时段取消了","改为三点的班车","收到","等通知"])]
    media=[row(200+i,1100+i,text="") for i in range(20)]
    noise=[row(300+i,200+i,"noise",text="另一话题") for i in range(40)]
    steps=[dict(window="discovery",record_refs=[r["id"] for r in history]),
        dict(window="episode",record_refs=[r["id"] for r in episode],support=[history[0]["id"]])]
    chosen,stats=select_evidence(history+episode+media+noise,steps,[],limit=12,byte_limit=16000,
        focus_scope=dict(sent_from=1000,sent_to=1200),seen_ids=[])
    assert {r["id"] for r in episode} <= {r["id"] for r in chosen}
    assert history[0]["id"] in {r["id"] for r in chosen}
    assert not any(r["conversation_id"]=="noise" or not r["quote"] for r in chosen)
    assert stats["undecoded_nontext_omitted"]==20
    shuffled=list(reversed(history+episode+media+noise))
    assert select_evidence(shuffled,steps,[],limit=12,byte_limit=16000,focus_scope=dict(sent_from=1000,sent_to=1200))[0]==chosen
    old_claim=claim("仍需等待",[episode[0]["id"]])
    newer=[row(500+i,1150+i,"later",text="后续说明") for i in range(3)]
    again,stats=select_evidence(history+episode+newer,steps,[old_claim],limit=12,byte_limit=16000,
        focus_scope=dict(sent_from=1000,sent_to=1200),seen_ids=[r["id"] for r in chosen])
    assert {r["id"] for r in newer} <= {r["id"] for r in again}
    assert episode[0]["id"] in {r["id"] for r in again}


def test_namesake_actor_from_other_namespace_is_rejected_before_selection():
    tools,requests=remote_tools([raw(source_account_id="other-owner",text="同名者的其他话题")])
    from allday_asr.v3.ports.chat_data import ChatDataError
    with pytest.raises(ChatDataError,match="CONTEXT_IDENTITY_MISMATCH"):
        tools.search_remote(SCOPE,[])
    assert not tools.result["evidence"]


@pytest.mark.parametrize("fragment,offset",[("明晚之前启程",1),("今天晚上19:30开会",0),("明天三点出发",1)])
def test_source_relative_event_day_is_anchored_to_message_timezone(fragment,offset):
    now=int(datetime.fromisoformat("2026-08-18T23:00:00+08:00").timestamp())
    a,b=source_relative_period(fragment,"Asia/Singapore",now)
    assert datetime.fromtimestamp(a,__import__('zoneinfo').ZoneInfo("Asia/Singapore")).day==18+offset
    assert b-a==86399


def test_schema_rejects_timestamp_in_date_field_without_silent_truncation():
    c=claim("据发言计划于次日启程",["E1"],event=dict(basis="relative",first="2026-08-19T03:00:00+08:00",last="2026-08-19",evidence_id="E1",quote="明天三点出发"))
    with pytest.raises(ValueError): checked(response("answer",claims=[c]))


def test_partial_month_day_requires_inference_and_message_time_is_not_evening():
    timestamp=int(datetime.fromisoformat("2026-08-18T16:30:00+08:00").timestamp())
    r=row(1,timestamp,text="8月20日开会");e={r["id"]:r};scope=dict(timezone="Asia/Singapore")
    event=dict(basis="explicit",first="2026-08-20",last="2026-08-20",evidence_id=r["id"],quote=r["quote"])
    with pytest.raises(ValueError):_claims({"claims":[claim("计划开会",[r["id"]],event=event)]},e,scope)
    event["basis"]="inferred"
    assert _claims({"claims":[claim("推测指当年八月二十日",[r["id"]],event=event)]},e,scope)
    event.update(basis="message",first="2026-08-18",last="2026-08-18")
    with pytest.raises(ValueError): _claims({"claims":[claim("当日晚上讨论",[r["id"]],event=event)]},e,scope)
    with pytest.raises(ValueError): _claims({"claims":[claim("2026-08-20确定开会",[r["id"]])]},e,scope)


def test_preflight_uses_each_windows_own_unix_bounds_and_current_handles(tmp_path):
    r=row(1,1000);r['id']='E1'
    p=dict(scope=dict(timezone="Asia/Singapore",sent_from=500,sent_to=2000),
        hard_scope=dict(sent_from=0,sent_to=3000),records=[r],objects=[],steps=[])
    prepared=prepare_payload(p)
    assert prepared['hard_scope']['sent_from_local'].startswith('1970-01-01')
    bad=deepcopy(p);bad['hard_scope']['sent_from_local']='2026-08-18T18:00:00+08:00'
    with pytest.raises(ValueError):prepare_payload(bad)
    bad=deepcopy(p);bad['objects']=[dict(evidence_ids=['E2'])]
    with pytest.raises(ValueError):prepare_payload(bad)
    runner=ModelExecutionRunner(tmp_path,max_prompt_bytes=60000)
    with pytest.raises(ModelExecutionTerminal,match='model request budget exceeded'):
        runner.prepare_request(task='offline',prompt='x'*60000,instructions='test',schema={},model='gpt-6.1-sol',effort='low')
    assert not list(tmp_path.glob('*.receipt.json'))



def test_decoded_cache_reads_are_cumulative_and_bounded_across_contexts():
    from allday_asr.v3.domain.recall_read_budget import reading,charge,scan_limit,ReadBudgetExceeded
    with reading() as budget:
        for _ in range(399):charge("x")
        assert scan_limit(100)==1
        charge("x")
        with pytest.raises(ReadBudgetExceeded):scan_limit()
        assert budget.records==400
    with reading() as budget:
        charge("x"*524288)
        with pytest.raises(ReadBudgetExceeded):charge("y")
        assert budget.bytes==524288


def test_independent_cross_platform_candidates_are_leads_not_a_person_merge(tmp_path,monkeypatch):
    from tests.test_recall_real_improvements import env as native_fixture
    from tests.test_recall_query_loop import ask
    native=native_fixture.__wrapped__(tmp_path,monkeypatch)
    seen=[]
    def model(p,c):
        seen.append(p)
        return response('answer',unknown=['虚构接口驱动仅核对独立来源，不给语义结论'])
    native.service.investigator=model
    result=ask(native,'昨天青岚准备如何去机场',expand=True)
    assert not result['contact_candidates'] and result['status']!='needs_clarification'
    assert seen and {r['source'] for r in seen[0]['records']}=={'qq','wechat'}
    assert all(not r.get('person_id') for r in result['evidence'])
    assert any('不据同名合并' in n for n in result['coverage'])



def test_preflight_maps_each_handle_to_the_exact_authorized_source_revision():
    from allday_asr.v3.application.recall_citation_handles import CitationHandles
    original=row(1,1000); handles=CitationHandles({});encoded=handles.encode([original])
    handles.validate(encoded,{original['id']:original})
    for changes in ({'quote':'伪造内容'},{'dataset':'other'},{'revision':2},{'record_id':'other'}):
        with pytest.raises(ValueError):handles.validate([{**encoded[0],**changes}],{original['id']:original})



def test_conversation_handles_are_exact_local_operands_never_native_id_guesses():
    from allday_asr.v3.application.recall_citation_handles import CitationHandles
    state={}; handles=CitationHandles(state)
    native={'ref':'contact:native-key','type':'contact'}
    c=handles.object(native); assert c=='C1' and handles.object(native)==c
    other=handles.object({'ref':'contact:other-namespace','type':'contact'});assert other=='C2'
    value=response(actions=[__import__('tests.test_agentic_recall',fromlist=['action']).action('search_messages',[c])])
    assert handles.decode(value)['actions'][0]['targets']==['contact:native-key']
    for bad in ('chat:native-conversation-id','contact:native-key','C999'):
        wrong=deepcopy(value);wrong['actions'][0]['targets']=[bad]
        with pytest.raises(ValueError):handles.decode(wrong)
    next_round=CitationHandles(state)
    with pytest.raises(ValueError):next_round.decode(value)


def test_negated_daypart_comparison_does_not_discard_supported_statement():
    from allday_asr.v3.domain.recall_event_time import validate_message_dayparts
    validate_message_dayparts("讨论在8月18日下午，并非前天晚上：甲说明班车安排。",16)
    validate_message_dayparts("不是昨天晚上，而是今天下午的发言。",16)
    validate_message_dayparts("讨论在8月18日下午，并非前天（19日）晚上：甲提出疑虑。",16)
    for text in ["8月18日晚上讨论班车。", "不是昨天下午而是今天晚上讨论。",
                 "下午讨论，晚上又更改班次。", "8月18日上午讨论班车。"]:
        with pytest.raises(ValueError,match="daypart"):
            validate_message_dayparts(text,16)


def test_continued_read_budget_keeps_prior_decoded_body_costs():
    from allday_asr.v3.domain.recall_read_budget import reading,charge,scan_limit,ReadBudgetExceeded
    with reading(records=399,bytes_read=100) as budget:
        assert scan_limit(20)==1
        charge("one")
        assert budget.records==400 and budget.bytes==103
        with pytest.raises(ReadBudgetExceeded):charge("two")


@pytest.mark.parametrize("left,final",[(2,True),(5,True),(7,False)])
def test_model_objects_require_presented_relation_proof_and_budget_reserves_summary(left,final):
    from allday_asr.v3.application.recall_investigation_input import payload
    from types import SimpleNamespace
    from threading import Event
    import time
    r=row(1,1100,text="虚构会议改期")
    r.update(conversation_type="group",source_is_from_self=False,speaker="乙")
    room=dict(ref="contact:room",type="contact",platform="qq",source_account_id="owner",
              conversation_id="new",relation_account=dict(platform="qq",source_account_id="owner",sender_account_id="peer"),
              relation_evidence=["old-unpresented"],relation_basis="observed_same_source_account_speech")
    hidden={**room,"ref":"contact:other","conversation_id":"other"}
    scope=dict(timezone="Asia/Singapore",sent_from=1000,sent_to=1200)
    result=dict(query_id="q",scope=scope,_request=dict(question="会议经过"),investigation_steps=[],
                evidence=[r],claims=[],metrics={},coverage=[])
    tools=SimpleNamespace(result=result,root_scope=scope,plan={},now=1300,refs={room["ref"]:room,hidden["ref"]:hidden},
        flow=SimpleNamespace(selected=lambda *a:None),deadline=time.monotonic()+80,
        remaining=lambda:dict(seconds=80,source_requests=left,body_requests=4),note=lambda s:None)
    value=payload(tools,2,"会议经过")
    assert value["final_round"] is final
    assert value["finalization_reason"]==("source_budget" if final else "")
    assert len(value["objects"])==1 and value["objects"][0]["relation_evidence"]==["E1"]
    assert tools.model_handles.object_allowed=={"C1":"contact:room"}


def test_exhausted_cache_does_not_cancel_authorized_remote_search():
    from allday_asr.v3.ports.chat_data import ChatDataError
    tools,requests=remote_tools([raw()])
    def exhausted(*args):raise ChatDataError("CACHE_BODY_BUDGET_REACHED",429)
    tools.service.sources.chat=exhausted
    target=tools.refs["contact:chosen"]
    action=dict(tool="search_messages",purpose="虚构称呼检索",targets=[target["ref"]],terms=[],window="target",evidence_ids=[])
    assert tools.search([target],action,SCOPE)==["chat:synthetic"]
    assert len(requests)==1 and tools.result["metrics"]["source_records_read"]==1
    assert any("不重置缓存计数" in n for n in tools.result["coverage"])
    def denied(*args):raise ChatDataError("AUTHORIZATION_REVOKED",403)
    tools.service.sources.chat=denied
    with pytest.raises(ChatDataError,match="AUTHORIZATION_REVOKED"):
        tools.search([target],action,SCOPE)
    assert len(requests)==1
