"""Synthetic actor-span regressions; native room labels are query context."""
import pytest
from allday_asr.v3.domain.recall_query import understand
from tests.recall_scenario_driver import contact, message, run_scenario, selected_ids
NOW=1792110900

def parse(question, names=()):
    people={str(i):dict(person_id=str(i),display_name=n,aliases=[],kind="contact") for i,n in enumerate(names)}
    return understand(question,"",people,{"source":"all","timezone":"Asia/Singapore"},NOW)

@pytest.mark.parametrize("noun",["这段聊天","那次聊天","的私聊","的对话"])
def test_demonstrative_conversation_is_not_part_of_native_contact(noun):
    p=parse("昨天我和微光摄影"+noun+"主要说了些什么？",["微光摄影"])
    assert p["person_mention"]=="微光摄影"
    assert p["scope"]["relationship"]=="conversation"
    assert not p["issue"]

@pytest.mark.parametrize("prefix",["“云岭摄影”群里", "云岭摄影群中", "在星海摄影群里"])
def test_room_before_speaker_remains_context_not_name(prefix):
    p=parse("昨天上午，"+prefix+"林岑聊了什么打算？")
    assert p["person_mention"]=="林岑"
    assert p["scope"]["relationship"]=="speaker"

@pytest.mark.parametrize("verb",["聊了什么","讨论了什么","交流了什么","发了什么"])
def test_room_after_speaker_is_not_in_unknown_name(verb):
    p=parse("昨天林岑在云岭摄影群"+verb)
    assert p["person_mention"]=="林岑"

def test_explicit_native_name_containing_room_words_is_preserved():
    p=parse("昨天人群里的微光聊了什么",["人群里的微光"])
    assert p["person_mention"]=="人群里的微光"

def test_multiple_people_do_not_become_a_single_supported_actor():
    p=parse("昨天我和林岑以及周启的私聊聊了什么",["林岑","周启"])
    assert p["issue"]
    assert not p["scope"]["person_id"]

def test_prefixed_room_full_pipeline_selects_native_speaker_without_person_merge(tmp_path):
    s=dict(question="昨天上午，“云岭摄影”群里林岑聊了什么打算？",
        contacts=[contact("wechat","ridge","云岭摄影",kind="group")],
        records=[message(1,"准备参加周末摄影练习，还没有确认成行。","native-sender","ridge","wechat",hour=8,label="林岑"),
                 message(2,"有冲突就下次再约。","other","ridge","wechat",hour=8,minute=1)],
        protocol_citation_probe=True)
    run=run_scenario(tmp_path,s)
    assert run["internal"]["_plan"]["person_mention"]=="林岑"
    assert "rec:fixture-1" in selected_ids(run)
    assert run["result"]["claims"]
    assert run["result"]["model_status"]=="available"
    assert not run["internal"]["scope"]["person_id"]
    assert not run["internal"]["subject_resolution"]["persistent_person_merge"]

@pytest.mark.parametrize("noun",["群聊","群里的大家"])
def test_generic_room_subject_does_not_become_an_unresolved_person(noun):
    p=parse("昨天"+noun+"讨论了什么")
    assert p["scope"]["relationship"]=="group"
    assert not p["person_mention"]

def test_explicit_group_actor_is_not_replaced_by_same_name_private_contact(tmp_path):
    s=dict(question="昨天上午，“云岭项目、星海小队、其他”群里林岑聊了什么打算？",
        contacts=[contact("qq","work-room","云岭项目、星海小队、其他",kind="group"),contact("qq","same-name-private","林岑","different-peer")],
        records=[message(50,"下周想参加摄影培训，还没报名。","group-actor","work-room",hour=8,label="林岑"),message(51,"你准备哪天去？","other","work-room",hour=8,minute=1),message(52,"先问问安排，周日不一定有空。","group-actor","work-room",hour=8,minute=2,label="林岑"),message(53,"私聊里讨论的是另一件无关的事。","different-peer","same-name-private",hour=8,kind="c2c",label="林岑")],protocol_citation_probe=True)
    run=run_scenario(tmp_path,s)
    assert {"rec:fixture-50","rec:fixture-51","rec:fixture-52"} <= selected_ids(run)
    assert "rec:fixture-53" not in selected_ids(run)
    assert run["internal"]["_plan"]["person_mention"]=="林岑"
    assert {c["conversation_id"] for c in run["internal"]["scope"]["source_contacts"]}=={"work-room"}
    assert all("云岭项目" not in t for t in run["internal"]["terms"])
    assert run["internal"]["subject_resolution"]["person_identity_confirmed"] is False

def test_unresolved_explicit_room_does_not_fall_back_to_same_label_private_peer(tmp_path):
    s=dict(question="昨天上午，“未覆盖的摄影组”群里林岑聊了什么？",contacts=[contact("qq","private","林岑","other-peer")],records=[message(60,"这是无关私聊，不能回答另一个群的问题。","other-peer","private",kind="c2c",label="林岑")],protocol_citation_probe=True)
    run=run_scenario(tmp_path,s)
    assert not run["wire"] and not selected_ids(run)
    assert run["result"]["status"]=="needs_clarification"
    assert not run["internal"]["scope"]["person_id"]

@pytest.mark.parametrize("operator",["这段聊天主要说了些什么","这次对话大概聊了些什么","的私聊都谈过什么"])
def test_plain_conversation_recap_operators_are_not_content_terms(operator):
    p=parse("昨天我和微光摄影"+operator+"？",["微光摄影"])
    assert p["person_mention"]=="微光摄影"
    assert p["intent"]=="conversation" and p["terms"]==[]

def test_plain_recap_uses_existing_single_summary_path(tmp_path):
    s=dict(question="昨天我和微光摄影这段聊天主要说了些什么？",contacts=[contact("wechat","private-room","微光摄影","peer")],records=[message(70,"你准备参加哪场摄影练习？","self-account","private-room","wechat",kind="c2c"),message(71,"周日先看看天气，还没有确认。","peer","private-room","wechat",kind="c2c",minute=1)],protocol_citation_probe=True)
    run=run_scenario(tmp_path,s)
    assert len(run["wire"])==1 and run["wire"][0]["task"]=="history-recall-v2"
    assert {"rec:fixture-70","rec:fixture-71"} <= selected_ids(run)
