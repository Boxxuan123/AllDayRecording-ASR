"""Explicit platform sets permit comparison while preserving every namespace boundary."""
from copy import deepcopy
from types import SimpleNamespace
import pytest
from allday_asr.v3.domain.recall_query import understand
from allday_asr.v3.domain.recall_investigation_scope import inside_root, intersect_root, remote_grants
from allday_asr.v3.adapters.sqlite.recall_sources import RecallSources
from tests.recall_scenario_driver import run_scenario,contact,message,selected_ids,NOW

def parse(q,**kw):
    return understand(q,"",{},dict(source="all",timezone="Asia/Singapore")|kw,NOW)

def root(**kw):
    return dict(source="all",person_id="",sent_from=0,sent_to=NOW,source_platforms=["qq","wechat"],timezone="Asia/Singapore")|kw

def test_multiple_named_platforms_are_source_set_not_identity_merge():
    p=parse("QQ和微信里的霁月能确认是同一个人吗？")
    assert p["scope"]["source"]=="all"
    assert p["scope"]["source_platforms"]==["qq","wechat"]
    assert not p["scope"]["person_id"]

@pytest.mark.parametrize("source",["qq","wechat","recording"])
def test_explicit_ui_single_platform_conflicts_with_two_named_platforms(source):
    with pytest.raises(ValueError,match="来源"):
        parse("QQ和微信里能确认霁月是谁吗？",source=source)

def test_literal_lookup_does_not_parse_quoted_source_names():
    q="QQ和微信里明晚开会"
    p=understand(q,q,{},dict(source="qq",timezone="Asia/Singapore"),NOW)
    assert p["intent"]=="quote" and p["scope"]["source"]=="qq"
    assert "source_platforms" not in p["scope"]

def test_dynamic_scope_cannot_drop_or_replace_explicit_source_set():
    hard=root(source_platforms=["qq","recording"])
    out=intersect_root(hard,root())
    assert out["source_platforms"]==["qq"]
    assert intersect_root(hard,root(source="wechat")) is None
    dynamic=root();dynamic.pop("source_platforms")
    assert intersect_root(hard,dynamic)["source_platforms"]==["qq","recording"]
    assert not inside_root(dict(platform="wechat"),hard)
    grants=[dict(platform=p,source_account_id="owner") for p in ["qq","wechat"]]
    assert [g["platform"] for g in remote_grants(hard,grants,[])]==["qq"]

def test_recordings_are_rejected_before_database_access_when_unrequested():
    class NoRead:
        def __getattr__(self,key): raise AssertionError("unrequested database access")
    repo=RecallSources(NoRead())
    assert repo.recordings(root(),[],20,None,0)==([],False)
    assert repo.chat(root(source="recording"),[],20,None,0)==([],False)

def test_independent_same_label_accounts_both_reach_real_request_without_person_merge(tmp_path):
    contacts=[contact(p,p+"-room","霁月",p+"-peer") for p in ["qq","wechat"]]
    s=dict(question="QQ和微信里的霁月能确认是同一个人吗？",contacts=contacts,
           records=[message(i+1,"还没有确认这两个来源的实名身份",p+"-peer",p+"-room",p,kind="c2c") for i,p in enumerate(["qq","wechat"])],protocol_citation_probe=True)
    run=run_scenario(tmp_path,s)
    assert run["wire"]
    assert {"rec:fixture-1","rec:fixture-2"} <= selected_ids(run)
    plan=run["internal"]["_plan"]
    assert plan["scope"]["source_platforms"]==["qq","wechat"]
    assert not plan["scope"]["person_id"]
    assert not run["internal"].get("subject_resolution",{}).get("persistent_person_merge")

def test_source_set_blocks_unrequested_platform_with_more_active_decoy(tmp_path):
    s=dict(question="昨天QQ和录音里的霁月准备参加什么活动？", contacts=[contact("qq","q","霁月","q-peer"),contact("wechat","w","霁月","w-peer")],records=[message(1,"准备参加摄影活动，还未确认。","q-peer","q","qq",kind="c2c")]+[message(100+i,"准备参加不相关的跑步活动。","w-peer","w","wechat",kind="c2c",minute=i) for i in range(30)],protocol_citation_probe=True)
    run=run_scenario(tmp_path,s)
    assert "rec:fixture-1" in selected_ids(run)
    assert not any(x.startswith("rec:fixture-1") and x!="rec:fixture-1" for x in selected_ids(run))
    for route,body,params in run["source_queries"]:
        assert params.get("scope",{}).get("platform")!="wechat"

def test_directory_only_identity_comparison_keeps_both_native_accounts_in_actual_request(tmp_path):
    s=dict(question="我想找霁月以前的聊天，QQ和微信里都出现这个名字，能把它们当成同一个人来回忆吗？", contacts=[contact(p,p+"-room","霁月",p+"-peer") for p in ["qq","wechat"]],records=[])
    run=run_scenario(tmp_path,s)
    assert run["wire"], "Metadata-supported uncertainty must reach bounded synthesis"
    import json
    payload=json.loads(run["wire"][-1]["prompt"])
    accounts={o.get("platform") for o in payload["objects"] if o.get("type")=="contact" and o.get("display_name")=="霁月"}
    assert {"qq","wechat"}==accounts
    native=run["internal"]["_plan"]["_source_candidates"]
    assert {(c["platform"],c["source_account_id"],c["conversation_id"]) for c in native}=={("qq","source-qq","qq-room"),("wechat","source-wechat","wechat-room")}
    refs=[o["ref"] for o in payload["objects"] if o.get("type")=="contact" and o.get("display_name")=="霁月"]
    assert len(set(refs))==2 and all(ref.startswith("C") for ref in refs)
    assert not run["internal"]["scope"]["person_id"]
    assert run["internal"]["scope"]["source_platforms"]==["qq","wechat"]
    assert not run["result"]["claims"]
