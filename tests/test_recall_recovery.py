"""Explicit recovery uses fictional source metadata/evidence, never live models."""

import copy
import time
import pytest
from tests.test_recall_contacts import native as native_fixture
from tests.test_recall_query_loop import ask, DAY, SUMMARY
from tests.test_history_recall import dump


@pytest.fixture
def native(tmp_path, monkeypatch):
    return native_fixture.__wrapped__(tmp_path, monkeypatch)


def action(env, result, name, **fields):
    return env.service.execute(
        "phone", {"action": name, "query_id": result["query_id"], **fields}
    )


def test_unknown_contact_has_action_without_model_or_message_search(native):
    native.service.planner = lambda *_: pytest.fail(
        "unknown name must not trigger speculative matching"
    )
    before = (dump(native.db.path), dump(native.cache.path))
    result = ask(native, "昨天我跟菱花聊了什么", source="wechat", expand=True)
    assert (
        result["status"] == "needs_clarification" and result["contact_lookup_allowed"]
    )
    assert not result["contact_candidates"] and not result["evidence"]
    choices = action(native, result, "contacts", search="Cobalt")
    assert len(choices["contact_candidates"]) == 1
    assert all(
        route not in {"/v1/messages", "/v1/search"}
        for route, _, _ in native.source.calls
    )
    continued = action(
        native, result, "choose", contact_key=choices["contact_candidates"][0]["key"]
    )
    assert (
        continued["query_id"] == result["query_id"] and continued["answer"] == SUMMARY
    )
    assert continued["scope"]["sent_from"] == DAY
    assert continued["scope"]["sent_to"] == DAY + 86400 - 1
    assert continued["scope"]["source"] == "wechat"
    assert {r["effective_is_self"] for r in continued["evidence"]} == {False, True}
    assert all(r["conversation_id"] == "native-wechat" for r in continued["evidence"])
    assert (
        not continued["scope"]["person_id"] and not continued["contact_lookup_allowed"]
    )
    assert (dump(native.db.path), dump(native.cache.path)) == before


def test_lookup_keeps_namespaces_and_rejects_unoffered_or_foreign_selection(native):
    result = ask(native, "昨天我跟菱花聊了什么", expand=True)
    choices = action(native, result, "contacts", search="青")
    assert {r["platform"] for r in choices["contact_candidates"]} == {"qq", "wechat"}
    with pytest.raises(ValueError):
        action(native, result, "choose", contact_key="f" * 32)
    with pytest.raises(ValueError):
        native.service.execute(
            "other", {"action": "contacts", "query_id": result["query_id"]}
        )
    action(native, result, "cancel")
    with pytest.raises(ValueError):
        action(native, result, "contacts")


def test_lookup_empty_offline_and_refresh_budget_are_truthful(native):
    result = ask(native, "昨天我跟菱花聊了什么", source="wechat", expand=True)
    original = native.service.directory.load
    seen = []

    def load(platform, live, *args):
        seen.append(live)
        return original(platform, live, *args)

    native.service.directory.load = load
    native.source.failure = "SOURCE_OFFLINE"
    choices = action(native, result, "contacts", search="不存在的备注")
    assert not choices["contact_candidates"] and "离线" in choices["notice"]
    assert choices["coverage"]
    for _ in range(4):
        action(native, result, "contacts")
    assert seen == [True, True, False, False, False]
    assert all(
        route not in {"/v1/messages", "/v1/search"}
        for route, _, _ in native.source.calls
    )


def test_contact_selection_cannot_bypass_unknown_time_or_person_filter(native):
    native.service.planner = lambda *_: (_ for _ in ()).throw(TimeoutError())
    result = ask(native, "那天我跟菱花聊了什么", source="wechat", expand=True)
    assert not result["contact_lookup_allowed"]
    with pytest.raises(ValueError):
        action(native, result, "contacts")
    assert not result["evidence"]


def test_lookup_paging_offers_only_current_real_page(native):
    native.contacts.extend(
        {
            **native.contacts[0],
            "id": f"extra-{i}",
            "remark": f"同事{i:03}",
            "nickname": f"peer{i:03}",
        }
        for i in range(26)
    )
    result = ask(native, "昨天我跟菱花聊了什么", source="wechat", expand=True)
    first = action(native, result, "contacts", search="同事")
    assert len(first["contact_candidates"]) == 20 and first["has_more"]
    second = action(
        native, result, "contacts", search="同事", offset=first["next_offset"]
    )
    assert len(second["contact_candidates"]) == 6 and not second["has_more"]
    assert not (
        {c["key"] for c in first["contact_candidates"]}
        & {c["key"] for c in second["contact_candidates"]}
    )
    with pytest.raises(ValueError):
        action(
            native, result, "choose", contact_key=first["contact_candidates"][0]["key"]
        )


def test_summary_longer_than_old_deadline_can_complete(native, monkeypatch):
    original = native.service.model
    stamp = [time.monotonic()]
    monkeypatch.setattr(time, "monotonic", lambda: stamp[0])

    def slow(payload, cancel):
        assert payload["model_budget_seconds"] == 45
        stamp[0] += 24
        return original(payload, cancel)

    native.service.model = slow
    result = ask(native, "昨天我跟Cobalt聊了什么", source="wechat", expand=True)
    assert result["model_status"] == "available" and result["answer"] == SUMMARY
    assert result["metrics"]["model_elapsed_ms"] == 24000
    assert not result["summary_retry_available"]


@pytest.mark.parametrize(
    "failure,status,reason",
    [
        (TimeoutError, "timeout", "超时"),
        (RuntimeError, "unavailable", "暂不可用"),
        (ValueError, "failed", "校验"),
    ],
)
def test_failure_reason_and_one_explicit_retry_reuses_original_evidence(
    native, failure, status, reason
):
    original = native.service.model
    native.service.model = lambda *_: (_ for _ in ()).throw(failure())
    result = ask(native, "昨天我跟Cobalt聊了什么", source="qq", expand=True)
    assert result["model_status"] == status and reason in result["answer"]
    assert result["summary_retry_available"] and not result["claims"]
    assert len(result["evidence"]) == 2
    calls, before = (
        copy.deepcopy(native.source.calls),
        (dump(native.db.path), dump(native.cache.path)),
    )
    received = []

    def retry(payload, cancel):
        received.append(payload)
        return original(payload, cancel)

    native.service.model = retry
    completed = action(native, result, "retry_summary")
    assert completed["answer"] == SUMMARY and completed["model_status"] == "available"
    assert (
        completed["scope"] == result["scope"]
        and completed["evidence"] == result["evidence"]
    )
    assert (
        received[0]["summary_attempt"] == 1
        and received[0]["model_budget_seconds"] == 45
    )
    assert native.source.calls == calls
    assert (dump(native.db.path), dump(native.cache.path)) == before
    assert not completed["summary_retry_available"]
    with pytest.raises(ValueError):
        action(native, result, "retry_summary")


def test_failed_retry_does_not_automatically_retry_again(native):
    native.service.model = lambda *_: (_ for _ in ()).throw(TimeoutError())
    result = ask(native, "昨天我跟Cobalt聊了什么", source="wechat", expand=True)
    again = action(native, result, "retry_summary")
    assert again["model_status"] == "timeout" and not again["summary_retry_available"]
    assert again["evidence"] == result["evidence"] and not again["claims"]
    assert again["metrics"]["model_calls"] == 2
    with pytest.raises(ValueError):
        action(native, result, "retry_summary")


def test_retry_cannot_revive_stale_evidence_or_another_owner(native):
    native.service.model = lambda *_: (_ for _ in ()).throw(TimeoutError())
    result = ask(native, "昨天我跟Cobalt聊了什么", source="wechat", expand=True)
    with pytest.raises(ValueError):
        native.service.execute(
            "other", {"action": "retry_summary", "query_id": result["query_id"]}
        )
    native.service.sources.changed = lambda rows: {rows[0]["id"]}
    with pytest.raises(ValueError):
        action(native, result, "retry_summary")
    assert action(native, result, "page", page=0)["status"] == "stale"


def test_retry_cancel_does_not_publish_result(native):
    native.service.model = lambda *_: (_ for _ in ()).throw(TimeoutError())
    result = ask(native, "昨天我跟Cobalt聊了什么", source="wechat", expand=True)

    def cancel(payload, event):
        action(native, result, "cancel")
        return {"claims": [], "unknown": []}

    native.service.model = cancel
    assert action(native, result, "retry_summary")["status"] == "cancelled"
    assert ("phone", result["query_id"]) not in native.service.results
    assert not native.service.active


def test_adapter_retry_has_new_bounded_job_batch_without_live_model(
    tmp_path, monkeypatch
):
    import json
    from threading import Event
    from types import SimpleNamespace
    from allday_asr.v3.adapters.codex import recall_model

    calls = []

    class Runner:
        def __init__(self, path, **limits):
            assert limits["timeout_seconds"] == 45
            assert limits["max_attempts"] == limits["max_batch_calls"] == 1
            assert limits["batch_deadline_seconds"] == 45

        def run(self, **request):
            calls.append(request)
            return SimpleNamespace(
                final_response=json.dumps({"claims": [], "unknown": []})
            )

    monkeypatch.setattr(recall_model, "ModelExecutionRunner", Runner)
    adapter = recall_model.RecallModelAdapter(tmp_path)
    for attempt in [0, 1]:
        adapter(
            {
                "query_id": "fixture-adapter-1",
                "model_budget_seconds": 45,
                "summary_attempt": attempt,
                "records": [],
            },
            Event(),
        )
    assert calls[0]["batch_id"] != calls[1]["batch_id"]
    assert calls[0]["prompt"] != calls[1]["prompt"]
