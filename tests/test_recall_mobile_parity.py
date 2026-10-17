"""Phone defaults must not turn natural event questions into literal message filters."""

from copy import deepcopy
import pytest
from tests.test_recall_acceptance_scenarios import plan_case
from tests.recall_scenario_driver import run_scenario, selected_ids, DAY
from tests.test_history_recall import env as env, query

from allday_asr.v3.domain.recall_investigation import window_scope


SCOPE = dict(
    person_id="",
    source="all",
    relationship="speaker",
    timezone="Asia/Singapore",
    sent_from=DAY + 18 * 3600,
    sent_to=DAY + 30 * 3600 - 1,
)


@pytest.mark.parametrize("keyword", ["", "  ", "\t"])
def test_empty_phone_keyword_keeps_daytime_plan_window(keyword):
    request = dict(
        question="昨天晚上林岑打算去哪里",
        keyword=keyword,
        source="all",
        person_id="",
        summarize=True,
        expand=True,
    )
    assert (
        window_scope(SCOPE, "message_day", request, DAY + 86400 + 3600)["sent_from"]
        == DAY
    )


@pytest.mark.parametrize(
    "filters",
    [
        dict(keyword="精确原话"),
        dict(sent_from=DAY + 18 * 3600),
        dict(sent_to=DAY + 30 * 3600 - 1),
    ],
)
def test_actual_explicit_filters_keep_message_window(filters):
    assert (
        window_scope(
            SCOPE,
            "message_day",
            dict(question="昨天晚上林岑打算去哪里", **filters),
            DAY + 2 * 86400,
        )["sent_from"]
        == SCOPE["sent_from"]
    )


@pytest.mark.parametrize("cached", [False, True])
def test_normal_phone_and_backend_payloads_reach_same_plan_evidence(tmp_path, cached):
    base = plan_case()
    base["cached_directory"] = cached
    phone = deepcopy(base)
    phone["filters"] = dict(
        keyword="",
        person_id="",
        source="all",
        timezone="Asia/Shanghai",
        summarize=True,
        expand=True,
    )
    backend = run_scenario(tmp_path / "backend", base)
    mobile = run_scenario(tmp_path / "mobile", phone)
    required = {"rec:fixture-3", "rec:fixture-4", "rec:fixture-5"}
    assert required <= selected_ids(backend)
    assert required <= selected_ids(mobile)
    assert selected_ids(backend) == selected_ids(mobile)


def test_original_context_uses_current_cumulative_limits_and_preserves_fallback(env):
    service, cache, db, rows, source = env
    value = query(service)
    saved = service.results[("phone", value["query_id"])][1]
    saved["metrics"].update(
        source_requests=17,
        retrieval_requests=11,
        body_requests=10,
        source_records_read=101,
        source_bytes=1000,
    )
    before = len(source.calls)
    context = service.context(
        "phone", value["query_id"], "chat:" + rows[0]["record_id"]
    )
    assert context["status"] == "available"
    assert len(source.calls) == before + 1
    assert saved["metrics"]["source_requests"] == 18
    assert saved["metrics"]["body_requests"] == 11
    assert saved["metrics"]["source_bytes"] > 1000
    assert any(e["quote"] == rows[0]["text"] for e in context["evidence"])
    fallback = service.context(
        "phone", value["query_id"], "chat:" + rows[0]["record_id"]
    )
    assert fallback["status"] == "unavailable" and "BUDGET" in fallback["notice"]
    assert len(source.calls) == before + 1
    assert len(fallback["evidence"]) > 1


@pytest.mark.parametrize(
    "exhausted",
    [
        dict(source_requests=18),
        dict(body_requests=12),
        dict(source_records_read=200),
        dict(source_bytes=1048576),
    ],
)
def test_original_context_cannot_reset_or_hide_any_physical_budget(env, exhausted):
    service, cache, db, rows, source = env
    value = query(service)
    saved = service.results[("phone", value["query_id"])][1]
    saved["metrics"].update(exhausted)
    before = len(source.calls)
    result = service.context("phone", value["query_id"], "chat:" + rows[0]["record_id"])
    assert result["status"] == "unavailable"
    assert len(source.calls) == before
    assert result["evidence"] and all(
        r["source"] == "qq" and r["conversation_id"] == rows[0]["conversation_id"]
        for r in result["evidence"]
    )


def test_original_context_small_remaining_record_budget_is_not_overdrawn(env):
    service, cache, db, rows, source = env
    value = query(service)
    saved = service.results[("phone", value["query_id"])][1]
    saved["metrics"].update(source_records_read=199)
    result = service.context("phone", value["query_id"], "chat:" + rows[0]["record_id"])
    assert result["status"] == "available"
    assert len(result["evidence"]) == 1
    assert saved["metrics"]["source_records_read"] == 200
