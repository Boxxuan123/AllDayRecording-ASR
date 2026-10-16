"""Unrelated synthetic acceptance and counterfactuals; fake transport is NOT semantic success."""
from copy import deepcopy
import random
from tests.recall_scenario_driver import contact,message,run_scenario,selected_ids,DAY
from allday_asr.v3.domain.recall_evidence_selection import select_evidence

def plan_case():
 return dict(question="昨天晚上林岑打算去哪里",contacts=[
  contact("wechat","wx-room","林岑","wx-peer"),contact("qq","qq-room","同学林岑","qq-peer"),
  contact("qq","workshop","读书小组",kind="group")],records=[
  message(1,"去年的培训资料先留着。","wx-peer","wx-room","wechat",day=DAY-180*86400,kind="c2c",label="林岑"),
  message(2,"今天的借阅通知已经看过了。","qq-peer","qq-room",kind="c2c",label="lin"),
  message(3,"晚一点你去哪儿？","other","workshop",minute=1),
  message(4,"图书馆旁的咖啡馆吧，还没动身。","qq-peer","workshop",minute=2,label="lin"),
  message(5,"如果下雨就不出门，先待定。","qq-peer","workshop",minute=3,label="lin")])

def test_cross_source_person_lead_and_daytime_plan_reaches_actual_request(tmp_path):
 scenario=plan_case();r=run_scenario(tmp_path,scenario)
 assert {"rec:fixture-3","rec:fixture-4","rec:fixture-5"}<=selected_ids(r)
 assert not r["result"]["contact_candidates"]
 assert r["wire"] and all(len(__import__('json').dumps(w,ensure_ascii=False).encode())<=60000 for w in r["wire"])
 assert not r["result"]["claims"]  # Offline probe never manufactures a semantic answer.

def test_explicit_platform_cannot_be_relaxed_for_recall(tmp_path):
 scenario=plan_case();scenario["filters"]={"source":"wechat"}
 r=run_scenario(tmp_path,scenario)
 assert all(x[2].get("scope",{}).get("platform")!="qq" for x in r["source_queries"])
 assert not ({"rec:fixture-4","rec:fixture-5"}&selected_ids(r))

def test_episode_density_cannot_evict_relevant_middle_question_and_answer():
 def row(i,t,room,text):return dict(id=str(i),record_id=str(i),revision=1,source="qq",source_account_id="owner",conversation_id=room,sender_account_id="peer",quote=text,sent_at=t)
 core=[row(100+i,1050+i,"target",s) for i,s in enumerate(["晚上的展板放哪里？","先放北侧入口","不，入口临时封了","换到南厅签到台旁","收到，等最终通知"])]
 noise=[row(i,1000+i,"noisy","游戏闲聊"*15) for i in range(90)]
 rows=noise+core
 chosen,stats=select_evidence(rows,[],[],limit=20,byte_limit=16000,focus_scope=dict(sent_from=900,sent_to=1200,question="昨天晚上的展板最后安排在哪里",terms=["展板"]),seen_ids=[])
 assert {r["id"] for r in core}<={r["id"] for r in chosen}
 random.Random(71).shuffle(rows)
 assert select_evidence(rows,[],[],limit=20,byte_limit=16000,focus_scope=dict(sent_from=900,sent_to=1200,question="昨天晚上的展板最后安排在哪里",terms=["展板"]),seen_ids=[])[0]==chosen


def bridge_case(broken=False):
 contacts=[contact("qq","lab","HERA评审组",kind="group"),
           contact("qq","room-target","三人讨论",kind="group"),contact("qq","room-noise","闲聊群",kind="group")]
 rows=[message(200,"请看通知，有问题稍后讨论。","actor-hera","lab",day=DAY-5*86400,label="Mira")]
 rows += [message(210+i,t,"actor-other" if broken else "actor-hera","room-target",minute=i,label="Mira")
          for i,t in enumerate(["展台最后放在哪里？","先看北门","北门封了，移到南厅","尚未执行，等开放通知","我记下了"]) ]
 rows += [message(300+i,"游戏闲聊，其他话题"*8,"actor-hera","room-noise",hour=15,minute=i//2,label="Mira") for i in range(90)]
 return dict(question="昨天晚上HERA发生了什么",contacts=contacts,records=rows,explore_related=True,cached_record_ids=[r["record_id"] for r in rows])

def test_d3_interference_received_and_accepted_but_must_reach_actual_request(tmp_path):
 r=run_scenario(tmp_path,bridge_case())
 core={f"rec:fixture-{210+i}" for i in range(5)}
 accepted=set(r["stage_ids"]["application_accepted"])
 assert core<=accepted  # This counterexample must expose selection, not missing source data.
 assert core<=selected_ids(r)

def test_same_names_without_native_bridge_do_not_authorize_target_room(tmp_path):
 r=run_scenario(tmp_path,bridge_case(broken=True))
 assert not {f"rec:fixture-{210+i}" for i in range(5)}&selected_ids(r)
