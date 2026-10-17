"""Syntax wrappers are not identities; full production flow, zero real SDK."""
import pytest
from allday_asr.v3.domain.recall_query import understand
from tests.recall_scenario_driver import contact, message, run_scenario, selected_ids
NOW=1792110900

def parse(q, names=()):
    people={str(i):dict(person_id=str(i),display_name=n,aliases=[],kind="contact") for i,n in enumerate(names)}
    return understand(q,"",people,{"source":"all","timezone":"Asia/Singapore"},NOW)

@pytest.mark.parametrize("prefix",["云岚摄影群关于", "“云岚摄影群”关于", "在云岚摄影群里关于", "“云岚摄影”群里关于"])
def test_group_topic_heading_is_not_a_person(prefix):
    p=parse("昨天，"+prefix+"镜头借用发了什么安排？")
    assert not p["person_mention"]
    assert p["room_mention"] in {"云岚摄影群","云岚摄影"}
    assert p["scope"]["relationship"]=="group"
    assert any("镜头借用" in t for t in p["terms"])

@pytest.mark.parametrize("wrapper",["备注“林的月”", "备注为“林的月”的", "昵称「林的月」的", "“林的月”的"])
def test_native_name_retains_internal_characters_not_outer_wrapper(wrapper):
    p=parse("昨天我和QQ"+wrapper+"那段私聊主要问了什么？",["林的月"])
    assert p["person_mention"]=="林的月"
    assert p["scope"]["source"]=="qq" and not p["issue"]

def test_unknown_quoted_remark_does_not_become_confirmed_identity():
    p=parse("昨天我和QQ备注为“陌生昵称”的那段私聊问了什么？")
    assert p["person_mention"]=="陌生昵称" and p["issue"]
    assert not p["scope"]["person_id"]

@pytest.mark.parametrize("name",['备注“林月”的','人群关于摄影'])
def test_full_confirmed_native_label_wins_over_wrapper_grammar(name):
    p=parse("昨天我和"+name+"的私聊聊了什么？",[name])
    assert p["person_mention"]==name


def test_explicit_group_topic_full_pipeline_keeps_room_boundary(tmp_path):
    scene=dict(question="昨天云岚摄影群关于镜头借用发了什么安排？是否已经归还？",
      contacts=[contact("qq","room","云岚摄影群",kind="group"),contact("qq","unrelated","云岚摄影群","different-peer")],
      records=[message(301,"镜头借用明天登记，归还还没有确认。","speaker","room"),message(302,"收到，先登记，不代表已经归还。","other","room",minute=1),message(303,"这是另一个人的无关私聊。","different-peer","unrelated",kind="c2c")],protocol_citation_probe=True)
    run=run_scenario(tmp_path,scene)
    assert not run["internal"]["_plan"]["person_mention"]
    assert run["internal"]["scope"]["relationship"]=="group"
    assert {"rec:fixture-301","rec:fixture-302"}<=selected_ids(run)
    assert "rec:fixture-303" not in selected_ids(run)
    assert run["result"]["claims"] and run["result"]["model_status"]=="available"
    assert not run["internal"]["scope"]["person_id"]


def test_quoted_remark_not_required_to_be_literal_message_content(tmp_path):
    scene=dict(question="昨天我和QQ备注“林的月”的那段私聊主要问了什么？",
      contacts=[contact("qq","private","林的月","peer")],
      records=[message(311,"旅途顺利吗？","peer","private",kind="c2c",label="小月")],protocol_citation_probe=True)
    run=run_scenario(tmp_path,scene)
    assert run["internal"]["_plan"]["person_mention"]=="林的月"
    assert "rec:fixture-311" in selected_ids(run)
    assert run["result"]["model_status"]=="available"
    assert not run["internal"]["scope"]["person_id"]


def test_missing_explicit_group_cannot_fall_back_to_same_label_private(tmp_path):
    scene=dict(question="昨天云岚摄影群关于镜头借用发了什么安排？",
      contacts=[contact("qq","private","云岚摄影群","peer")],
      records=[message(321,"别的来源内容。","peer","private",kind="c2c")],protocol_citation_probe=True)
    run=run_scenario(tmp_path,scene)
    assert not selected_ids(run) and not run["wire"]
    assert run["result"]["status"]=="needs_clarification"


def test_same_quoted_remark_across_native_accounts_is_not_merged(tmp_path):
    a=contact("qq","one","林的月","peer-one");b=contact("qq","two","林的月","peer-two");b['source_account_id']='different-native-owner'
    run=run_scenario(tmp_path,dict(question="昨天我和QQ备注“林的月”的那段私聊主要问了什么？",contacts=[a,b],records=[],protocol_citation_probe=True))
    assert not run["internal"]["scope"]["person_id"]
    # Same labels are independent discovery candidates, never a permanent merge.
    plan=run["internal"]["_plan"]
    assert plan["person_mention"]=="林的月"
    candidates=plan.get("_source_candidates",[]) or plan.get("contact_candidates",[])
    assert len({c["source_account_id"] for c in candidates})==2
    assert not run["result"]["claims"]
