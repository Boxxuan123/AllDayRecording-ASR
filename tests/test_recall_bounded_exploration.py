"""Quota gaps must not discard acquired evidence; identity questions are semantic."""

import pytest
from tests.recall_scenario_driver import run_scenario, selected_ids
from tests.test_recall_acceptance_scenarios import bridge_case, plan_case
from allday_asr.v3.adapters.sqlite.recall_sources import RecallSources
from allday_asr.v3.domain.recall_read_budget import CURRENT
from allday_asr.v3.ports.chat_data import ChatDataError
from allday_asr.v3.domain.recall_language import intent


def test_cache_exhaustion_still_presents_acquired_records_for_synthesis(
    tmp_path, monkeypatch
):
    def exhausted(*args, **kw):
        budget = CURRENT.get()
        budget.records = 400
        raise ChatDataError("CACHE_RECORD_BUDGET_REACHED", 429)

    monkeypatch.setattr(RecallSources, "related_conversations", exhausted)
    scene = bridge_case()
    scene["protocol_citation_probe"] = True
    run = run_scenario(tmp_path, scene)
    assert run["wire"], (
        "400 decoded records limits cache exploration, not the already funded model"
    )
    assert len(selected_ids(run)) > 0
    assert run["result"]["metrics"]["cache_scan_records"] == 400
    assert any(
        d["code"] == "CACHE_RECORD_BUDGET_REACHED"
        for d in run["internal"]["diagnostics"]
    )
    assert run["result"]["metrics"]["source_requests"] <= 18
    assert run["result"]["metrics"]["source_records_read"] <= 200


@pytest.mark.parametrize(
    "question,expected",
    [
        ("那位同事说的青石是哪位同学", "question"),
        ("请逐字找包含哪位老师的原话", "quote"),
        ("昨天为什么取消会议", "reason"),
        ("昨天活动发生了什么", "timeline"),
        ("昨天聊了什么", "conversation"),
        ("这件事最后怎么样了", "latest"),
    ],
)
def test_reference_identity_question_does_not_become_literal_lookup(question, expected):
    assert intent(question) == expected


def test_conversation_lookup_uses_question_specificity_not_directory_order():
    from allday_asr.v3.application.recall_investigation_tools import InvestigationTools

    def row(i, name):
        return dict(
            ref="contact:" + str(i),
            type="contact",
            conversation_type="group",
            names=[name],
            platform="qq",
            source_account_id="owner",
            key=str(i),
        )

    values = [row(i, "NOVA游戏群" + str(i)) for i in range(25)] + [
        row(99, "NOVA摄影群")
    ]
    t = InvestigationTools.__new__(InvestigationTools)
    t.refs = {v["ref"]: v for v in values}
    t.seen = set()
    t.calls = 0
    t.result = dict(
        _request={"question": "昨天NOVA和VEGA摄影群聊了什么"},
        evidence=[],
        metrics={},
        investigation_steps=[],
    )
    t.scope = lambda _: dict(sent_from=0, sent_to=10)
    t.checkpoint = lambda: None
    t.note = lambda _: None
    action = dict(
        tool="find_conversations",
        terms=["NOVA"],
        targets=[],
        evidence_ids=[],
        window="target",
        purpose="synthetic directory ranking",
    )
    refs = t.run(action, [])
    assert refs[0] == "contact:99"
    # Metadata order changes do not change which native candidate gets the first bounded read.
    t.seen = set()
    t.refs = {v["ref"]: v for v in reversed(values)}
    assert t.run(action, []) == refs


def test_identity_question_uses_cross_source_candidates_in_actual_request(tmp_path):
    scene = plan_case()
    scene["question"] = "昨天林岑提到的青石是哪位同学"
    scene["records"][2]["text"] = "青石是谁，我们认识吗？"
    scene["records"][3]["text"] = "我只知道这个外号，没确认真实姓名。"
    scene["records"][4]["text"] = "先别当成正式身份结论。"
    run = run_scenario(tmp_path, scene)
    assert {"rec:fixture-3", "rec:fixture-4", "rec:fixture-5"} <= selected_ids(run)
    assert not run["internal"]["scope"].get("person_id")
    assert run["internal"]["_plan"]["intent"] == "question"


def test_cache_quota_handler_does_not_swallow_authorization_error(
    tmp_path, monkeypatch
):
    def denied(*args, **kw):
        raise ChatDataError("AUTHORIZATION_REVOKED", 403)

    monkeypatch.setattr(RecallSources, "related_conversations", denied)
    run = run_scenario(tmp_path, bridge_case())
    assert not run["wire"]
    assert not run["result"]["claims"]
    assert not any(
        d.get("stage") == "relationship_cache"
        for d in run["internal"].get("diagnostics", [])
    )
