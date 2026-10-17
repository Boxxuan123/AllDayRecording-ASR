"""Deterministic fictional regressions for entry, semantic context, time and references."""
import copy
from datetime import datetime
import pytest
from tests.test_recall_real_improvements import env
from tests.test_recall_query_loop import ask
from tests.test_agentic_review_fixes import remote_tools, raw, SCOPE
from allday_asr.v3.domain.recall_language import plan
from allday_asr.v3.application.recall_citation_handles import CitationHandles
from allday_asr.v3.application.recall_investigation_budget import normalize_scope


def test_unfinished_other_platform_is_coverage_not_an_identity_conflict(env):
    directory, notes, _ = env.service.directory.load("all", True, __import__("threading").Event(), __import__("time").monotonic()+10)
    rows = [r for r in directory if r["platform"] == "wechat"]
    for r in rows: r["directory_complete"] = True
    env.service.directory.load = lambda *a: (copy.deepcopy(rows), ["QQ directory incomplete"], 0)
    result = ask(env, "昨天我和青岚聊了什么", expand=True)
    assert not result["contact_candidates"]
    assert result["model_status"] == "available"
    assert {r["conversation_id"] for r in result["evidence"]} == {"native-wechat"}
    assert "QQ directory incomplete" in result["coverage"]


@pytest.mark.parametrize("question", ["昨天松杉准备怎么去机场", "前天海棠打算参加哪个会议", "昨天紫苑的出行安排是什么"])
def test_attribute_questions_are_exploratory_not_exact_quotes(question):
    value = plan(question)
    assert value["intent"] == "question" and not value["exact"]
    assert plan(question, "下午三点火车")["exact"]
    assert plan("找出包含原话的消息")["intent"] == "quote"


def test_short_answer_keeps_other_speakers_question_denial_and_correction():
    tools, _ = remote_tools([])
    target = dict(id="chat:answer", record_id="answer", source="qq", source_account_id="owner-qq", conversation_id="chosen-c2c", sent_at=1000, quote="坐火车", revision=1)
    rows = [dict(target, id=str(i), record_id=str(i), sent_at=990+i, quote=t, sender_account_id=str(i))
            for i,t in enumerate(["你明天怎么去机场？", "坐火车", "不，刚才说错了", "改成下午三点的班车"])]
    kept = tools.context_rows(rows, target, SCOPE, ["出行计划"])
    assert len(kept) == 4
    assert {r["retrieval_basis"] for r in kept} == {"bounded_conversation_context"}


def test_window_calculation_never_claims_it_found_an_earlier_event():
    tools, _ = remote_tools([])
    tools.result["_request"]["question"] = "前天晚上观测社发生了什么"
    first = int(datetime.fromisoformat("2026-08-18T18:00:00+08:00").timestamp())
    tools.result["scope"].update(sent_from=first, sent_to=first+21600-1)
    tools.root_scope.update(sent_from=0, sent_to=first+86400)
    tools.now = first+86400
    value = tools.scope("nearby")
    assert "time_notice" not in tools.result and not tools.result["evidence"]
    assert "2026-08-17" in value["sent_from_local"]
    value.update(sent_from=first)
    normalize_scope(value)
    assert datetime.fromisoformat(value["sent_from_local"]).timestamp() == first


def test_handles_never_reassign_or_accept_an_unseen_or_other_query_citation():
    row = dict(id="chat:full-a", dataset="D", revision=1)
    result = {}; first = CitationHandles(result); assert first.encode([row])[0]["id"] == "E1"
    second = CitationHandles(result); assert second.encode([dict(row,id="chat:full-b")])[0]["id"] == "E2"
    with pytest.raises(ValueError): second.decode({"claims":[{"citations":["E1"]}]})
    with pytest.raises(ValueError): first.decode({"claims":[{"citations":["E9"]}]})
    revised = CitationHandles(result); assert revised.encode([dict(row,revision=2)])[0]["id"] == "E3"
    isolated = CitationHandles({}); assert isolated.encode([dict(row,id="other")])[0]["id"] == "E1"
    assert first.decode({"claims":[{"citations":["E1"]}]})["claims"][0]["citations"] == ["chat:full-a"]


def test_physical_fanout_and_record_budget_are_visible_and_enforced():
    tools, requests = remote_tools([raw()]); tools.current_window = "target"
    tools.result["metrics"]["body_requests"] = 12
    from allday_asr.v3.ports.chat_data import ChatDataError
    with pytest.raises(ChatDataError): tools.search_remote(SCOPE, [])
    assert requests == [] and tools.remaining()["body_requests"] == 0
