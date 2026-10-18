"""Final synthesis uses the existing query deadline, without granting another turn."""

from types import SimpleNamespace
from threading import Event
import json
import pytest
from allday_asr.v3.application import recall_investigation_input as module
from allday_asr.v3.adapters.codex import recall_investigator as adapter_module


def production_payload(monkeypatch, seconds, source_left, evidence=()):
    monkeypatch.setattr(module.time, "monotonic", lambda: 100.0)
    scope = dict(timezone="Asia/Singapore", sent_from=1000, sent_to=1200)
    result = dict(
        query_id="deadline-fixture",
        scope=scope,
        _request=dict(question="会议讨论经过"),
        investigation_steps=[],
        evidence=list(evidence),
        claims=[],
        metrics={},
        coverage=[],
    )
    tools = SimpleNamespace(
        result=result,
        root_scope=scope,
        plan={},
        now=1300,
        refs={},
        flow=SimpleNamespace(selected=lambda *a: None),
        deadline=100 + seconds,
        remaining=lambda: dict(
            seconds=seconds, source_requests=source_left, body_requests=4
        ),
        note=lambda s: None,
    )
    return module.payload(tools, 2, "会议讨论经过")


@pytest.mark.parametrize(
    "left,seconds,expected",
    [(1, 78, 45), (7, 78, 25), (1, 20, 18), (1, 2, 0), (1, 0, 0), (7, 100, 25)],
)
def test_final_wait_allocates_remaining_query_time_with_two_second_reserve(
    monkeypatch, left, seconds, expected
):
    value = production_payload(monkeypatch, seconds, left)
    assert value["model_budget_seconds"] == expected
    assert value["final_round"] is (left <= 2 or seconds < 30)
    assert value["budget_remaining"]["seconds"] == seconds
    assert value["rounds_remaining"] == 2


def install_runner(monkeypatch, duration=0):
    seen = {}

    class Runner:
        def __init__(self, root, **limits):
            seen.update(limits)

        def prepare_request(self, **kw):
            seen["prepared"] = True

        def run(self, **kw):
            seen["cancel"] = kw["cancel_event"]
            if seen["timeout_seconds"] < duration:
                raise TimeoutError("synthetic completion after 30 seconds")
            out = dict(claims=[], candidates=[], unknown=["protocol-only"])
            if "mode" in kw["schema"]["properties"]:
                out.update(
                    mode="answer",
                    time=dict(kind="unchanged", first="", last=""),
                    actions=[],
                )
            return SimpleNamespace(final_response=json.dumps(out))

    monkeypatch.setattr(adapter_module, "ModelExecutionRunner", Runner)
    return seen


def adapter_payload(final=True, seconds=45):
    return dict(
        scope=dict(timezone="Asia/Singapore", sent_from=0, sent_to=100),
        records=[],
        round=2,
        final_round=final,
        query_id="deadline-fixture",
        model_budget_seconds=seconds,
    )


def test_final_adapter_allows_synthetic_completion_after_old_25_second_cutoff(
    tmp_path, monkeypatch
):
    seen = install_runner(monkeypatch, duration=30)
    cancel = Event()
    result = adapter_module.RecallInvestigatorAdapter(tmp_path)(
        adapter_payload(), cancel
    )
    assert result["mode"] == "answer" and result["actions"] == []
    assert seen["timeout_seconds"] == 45 and seen["cancel"] is cancel
    assert seen["max_attempts"] == 1 and seen["max_batch_calls"] == 4
    assert seen["batch_deadline_seconds"] == 90 and seen["max_prompt_bytes"] == 60000


@pytest.mark.parametrize(
    "final,seconds,cap",
    [(True, 999, 45), (True, 18, 18), (False, 999, 25), (False, 12, 12)],
)
def test_adapter_clamps_individual_wait_without_expanding_query_budget(
    tmp_path, monkeypatch, final, seconds, cap
):
    seen = install_runner(monkeypatch)
    adapter_module.RecallInvestigatorAdapter(tmp_path)(
        adapter_payload(final, seconds), Event()
    )
    assert seen["timeout_seconds"] == cap
    assert (
        seen["max_attempts"] == 1
        and seen["max_batch_calls"] == 4
        and seen["batch_deadline_seconds"] == 90
    )


def test_expired_query_does_not_coerce_zero_remaining_time_to_paid_call(
    tmp_path, monkeypatch
):
    seen = install_runner(monkeypatch)
    with pytest.raises(TimeoutError, match="query deadline"):
        adapter_module.RecallInvestigatorAdapter(tmp_path)(
            adapter_payload(seconds=0), Event()
        )
    assert not seen


def native_review_row():
    return dict(
        id="chat:synthetic-review",
        record_id="synthetic-review",
        revision=1,
        source="qq",
        source_account_id="owner",
        conversation_id="room",
        conversation_type="group",
        sender_account_id="peer",
        quote="仍只是计划，尚未确定。",
        sent_at=1100,
        dataset="synthetic",
        source_is_from_self=False,
        effective_is_self=False,
        origin="chat",
        role="context",
    )


@pytest.mark.parametrize(
    "remaining,expected,final",
    [
        (88, 45, False),
        (68, 36, False),
        (50, 18, False),
        (42, 10, False),
        (41, 39, True),
        (20, 18, True),
        (2, 0, True),
    ],
)
def test_reading_evidence_has_time_without_consuming_final_reserve(
    monkeypatch, remaining, expected, final
):
    value = production_payload(monkeypatch, remaining, 7, [native_review_row()])
    assert value["records"]
    assert value["model_budget_seconds"] == expected
    assert value["final_round"] is final
    if not final:
        assert remaining - value["model_budget_seconds"] >= 32


def test_nonfinal_evidence_review_can_finish_after_the_old_planning_cutoff(
    tmp_path, monkeypatch
):
    value = production_payload(monkeypatch, 88, 7, [native_review_row()])
    assert not value["final_round"]
    seen = install_runner(monkeypatch, duration=30)
    adapter_module.RecallInvestigatorAdapter(tmp_path)(value, Event())
    assert seen["timeout_seconds"] == 45
    assert seen["max_attempts"] == 1 and seen["max_batch_calls"] == 4
    assert seen["batch_deadline_seconds"] == 90 and seen["max_prompt_bytes"] == 60000


def test_late_discovery_read_does_not_spend_final_synthesis_time():
    from allday_asr.v3.application.recall_investigation_budget import (
        InvestigationBudget,
    )
    from allday_asr.v3.ports.chat_data import ChatDataError

    class Source:
        timeout = 3
        calls = 0

        def request(self, *args, **kwargs):
            self.calls += 1
            return {"items": []}

    class Budget(InvestigationBudget):
        def checkpoint(self):
            pass

        def remaining(self):
            return dict(
                source_requests=18,
                body_requests=12,
                remote_records=200,
                remote_bytes=1048576,
                seconds=34,
            )

    b = Budget()
    source = Source()
    b.service = SimpleNamespace(source=source)
    b.result = dict(
        metrics=dict(model_calls=1, source_requests=0, retrieval_requests=0)
    )
    with pytest.raises(ChatDataError) as error:
        b.remote("/v1/messages", scope={})
    assert error.value.code == "SOURCE_TIME_RESERVED_FOR_FINAL"
    assert source.calls == 0 and b.result["metrics"]["source_requests"] == 0
