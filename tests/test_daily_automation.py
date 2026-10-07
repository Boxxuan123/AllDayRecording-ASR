from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from allday_asr.v3.adapters.sqlite import V3Database
from allday_asr.v3.adapters.sqlite.daily_generation_queue import (
    DailyGenerationQueue,
    day_worker_lock,
)
from allday_asr.v3.application.daily_inventory import (
    classify_day,
    DailyHistoryInventory,
)
from allday_asr.v3.application.daily_automation import DailyGenerationCoordinator
from allday_asr.v3.application.daily_backfill import DailyBackfillPass
from allday_asr.v3.domain.daily_semantics import SEMANTIC_VERSION


def item(day="2026-01-01", fingerprint="source-a", **updates):
    result = {
        "date": day,
        "timezone": "Asia/Singapore",
        "source_fingerprint": fingerprint,
        "generation_version": "approved-version",
        "classification": "MISSING",
        "needs_backfill": True,
        "upstream_complete": True,
        "active_utterance_count": 1,
        "existing_final_event_count": 0,
        "existing_summary_present": False,
        "summary_id": None,
        "summary_revision": None,
        "reason": "Missing",
    }
    return result | updates


@pytest.fixture
def queue(tmp_path):
    db = V3Database.open(tmp_path / "core.sqlite3")
    clock = SimpleNamespace(value=datetime(2026, 1, 2, tzinfo=timezone.utc))
    return DailyGenerationQueue(db, lambda: clock.value), clock


@pytest.mark.parametrize(
    "updates,current,changed,expected",
    [
        ({}, False, False, "MISSING"),
        ({"existing_summary_present": True}, False, False, "LEGACY_ONLY"),
        ({"existing_summary_present": True}, True, False, "CURRENT_COMPLETE_EMPTY"),
        ({"existing_final_event_count": 2}, True, False, "CURRENT_COMPLETE"),
        ({"existing_summary_present": True}, True, True, "STALE_SOURCE"),
        ({"active_utterance_count": 0}, False, False, "NO_SOURCE"),
        ({"upstream_complete": False}, False, False, "UPSTREAM_INCOMPLETE"),
    ],
)
def test_inventory_classification(updates, current, changed, expected):
    assert (
        classify_day(
            item(**updates),
            None,
            current_product=current,
            source_changed=changed,
            today="2026-01-02",
        )[0]
        == expected
    )


@pytest.mark.parametrize("trigger", ["startup", "receiver_sync"])
def test_repeated_trigger_enqueues_once(queue, trigger):
    q, clock = queue
    coordinator = DailyGenerationCoordinator(
        q.database, lambda _: None, "approved-version", now=lambda: clock.value
    )
    coordinator.inventory.scan = lambda **_: [item()]
    assert len(coordinator.catch_up(trigger)["created_job_ids"]) == 1
    assert not coordinator.catch_up(trigger)["created_job_ids"]
    assert len(q.states()) == 1


def test_today_rollover_only_becomes_pending(queue):
    q, _ = queue
    q.observe(
        [
            item(
                classification="STALE_SOURCE",
                needs_backfill=False,
                reason="Current day stays dirty until local rollover",
            )
        ]
    )
    assert q.states()[0]["status"] == "DIRTY_SOURCE_CHANGED"
    assert q.claim([item(needs_backfill=False)]) is None
    q.observe([item()])
    assert q.states()[0]["status"] == "PENDING"


@pytest.mark.parametrize(
    "classification", ["CURRENT_COMPLETE", "CURRENT_COMPLETE_EMPTY"]
)
def test_current_products_no_generation(queue, classification):
    q, _ = queue
    q.observe(
        [
            item(
                classification=classification,
                needs_backfill=False,
                summary_id="summary",
                summary_revision=1,
            )
        ]
    )
    assert q.states()[0]["status"] == "SUCCEEDED"
    assert q.claim([item(classification=classification, needs_backfill=False)]) is None


def test_two_failures_terminal_restart_counter_preserved(queue):
    q, clock = queue
    q.observe([item()])
    first = q.claim([item()])
    assert (
        q.finish(first, {}, success=False, error="protocol_invalid")
        == "FAILED_RETRYABLE"
    )
    clock.value += timedelta(minutes=6)
    restarted = DailyGenerationQueue(q.database, lambda: clock.value)
    restarted.observe([item(classification="FAILED_RESUMABLE")])
    second = restarted.claim([item(classification="FAILED_RESUMABLE")])
    assert second["attempts"] == 2
    assert (
        restarted.finish(second, {}, success=False, error="capacity")
        == "FAILED_TERMINAL"
    )
    restarted.observe([item(classification="FAILED_TERMINAL", needs_backfill=False)])
    assert restarted.claim([item()]) is None
    assert restarted.states()[0]["attempts"] == 2
    restarted.observe([item(fingerprint="source-revision-2")])
    assert restarted.claim([item(fingerprint="source-revision-2")])["attempts"] == 1


def test_generation_version_change_has_new_identity(queue):
    q, _ = queue
    q.observe([item(classification="CURRENT_COMPLETE", needs_backfill=False)])
    q.observe([item(generation_version="new-approved-version")])
    assert q.claim([item(generation_version="new-approved-version")])["attempts"] == 1


def test_abandoned_running_and_cross_process_exclusion(queue):
    q, _ = queue
    q.observe([item()])
    q.claim([item()])
    with day_worker_lock(q.database.path) as first:
        assert first
        with day_worker_lock(q.database.path) as second:
            assert not second
        assert q.recover_abandoned() == 1
    assert q.states()[0]["status"] == "FAILED_RETRYABLE"
    assert q.states()[0]["attempts"] == 1


def test_backfill_failure_not_retried_on_startup(queue):
    q, clock = queue
    q.observe([item()], origin="HISTORICAL")
    job = q.claim([item()], backfill=True)
    assert q.finish(job, {}, success=False) == "FAILED_TERMINAL"
    clock.value += timedelta(days=1)
    q.observe([item(classification="FAILED_TERMINAL", needs_backfill=False)])
    assert q.claim([item()]) is None
    q.retry(item()["date"], "approved-version")
    retry = q.claim([item()])
    assert retry["attempts"] == 2 and retry["attempt_limit"] == 3


def test_recent_day_priority_over_backlog(queue):
    q, _ = queue
    old = item(day="2025-12-30")
    new = item()
    q.observe([old], origin="HISTORICAL")
    q.observe([new])
    assert q.claim([old, new])["local_date"] == new["date"]


def test_queue_does_not_write_protected_domain(queue):
    q, _ = queue

    def protected():
        with q.database.read() as c:
            return {
                name: [tuple(r) for r in c.execute("SELECT * FROM " + name)]
                for name in (
                    "persons",
                    "event_current_states",
                    "reminder_schedules",
                    "speaker_research_usage",
                    "annotation_facts",
                )
            }

    before = protected()
    q.observe([item()])
    j = q.claim([item()])
    q.finish(j, {}, success=False)
    assert protected() == before


def test_formal_coordinator_runs_service_once_and_resumes_same_service_path(queue):
    q, clock = queue
    accepted = {"request1": False}
    calls = []

    class Service:
        def refresh_daily(self, *_):
            if not accepted["request1"]:
                calls.append("request1")
                accepted["request1"] = True
            calls.append("request2")
            return {"error": "semantic_generation_failed"}

        def close(self):
            pass

    worker = DailyGenerationCoordinator(
        q.database, lambda _: Service(), "approved-version", now=lambda: clock.value
    )
    worker.inventory.scan = lambda **_: [item()]
    worker.catch_up("startup")
    assert not worker.run_once()["generation_completed"]
    clock.value += timedelta(minutes=6)
    worker.catch_up("receiver_sync")
    assert not worker.run_once()["generation_completed"]
    assert calls == ["request1", "request2", "request2"]
    assert worker.run_once() is None


def test_finite_backfill_never_reinvokes_failed_date(queue, tmp_path):
    q, _ = queue
    calls = []
    worker = SimpleNamespace(inventory=SimpleNamespace(scan=lambda: [item()]))
    worker.run_once = lambda **_: (
        calls.append("run")
        or {"generation_completed": False, "publish_completed": False}
    )
    sealed = q.freeze_manifest([item()])
    runner = DailyBackfillPass(worker, sealed, tmp_path / "results.json")
    assert runner.run()["BACKFILL_PASS_COMPLETE"]
    assert DailyBackfillPass(worker, sealed, tmp_path / "results.json").run()[
        "BACKFILL_PASS_COMPLETE"
    ]
    assert calls == ["run"]


def test_inventory_metadata_source_revision_and_upstream(tmp_path):
    from tests.test_v32_three_layer_knowledge import (
        V32ThreeLayerKnowledgeTests,
        UTTERANCE_ID,
    )

    seed = V32ThreeLayerKnowledgeTests()
    seed.setUp()
    try:

        def clock():
            return seed.now + timedelta(days=2)

        inventory = DailyHistoryInventory(seed.database, "version", now=clock)
        first = inventory.scan()[0]
        assert first["classification"] == "MISSING"
        with seed.database.transaction() as c:
            c.execute(
                "UPDATE utterances SET revision=revision+1 WHERE utterance_id=?",
                (UTTERANCE_ID,),
            )
        assert inventory.scan()[0]["source_fingerprint"] != first["source_fingerprint"]
        with seed.database.transaction() as c:
            c.execute("UPDATE recording_sessions SET status_code='processing'")
        assert inventory.scan()[0]["classification"] == "UPSTREAM_INCOMPLETE"
    finally:
        seed.tearDown()


def test_coordinator_rejects_legacy_success(queue):
    q, clock = queue
    service = SimpleNamespace(
        refresh_daily=lambda *_: {
            "objective": {
                "semantic_status": "complete",
                "generation_version": "daily-local-v1",
            }
        },
        close=lambda: None,
    )
    worker = DailyGenerationCoordinator(
        q.database, lambda _: service, "approved-version", now=lambda: clock.value
    )
    worker.inventory.scan = lambda **_: [item()]
    assert not worker.run_once()["generation_completed"]
    assert SEMANTIC_VERSION != "daily-local-v1"


def test_isolated_startup_to_atomic_publish_and_clean_restart():
    from tests.test_v32_three_layer_knowledge import V32ThreeLayerKnowledgeTests
    from tests.test_daily_semantics import AnonymousAnalyzer
    from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
    from allday_asr.v3.application.insights import DailyInsightService
    from allday_asr.v3.ports.daily_semantics import DailyOverviewResult

    seed = V32ThreeLayerKnowledgeTests()
    seed.setUp()

    class Analyzer(AnonymousAnalyzer):
        model_label = "gpt-5.6-luna"
        overview_prompt_version = "daily-semantic-v1.2-overview-prompt.1"
        overview_schema_version = "daily-semantic-v1.2-overview-schema.2"

        def overview(self, request):
            ids = [v["event_id"] for v in request["major_events"]]
            return DailyOverviewResult(
                {
                    "headline": "匿名验证安排",
                    "headline_source_event_ids": ids,
                    "overview_sentences": [
                        {"text": "讨论匿名验证安排。", "source_event_ids": ids}
                    ],
                },
                {
                    "model": self.model_label,
                    "prompt_version": self.overview_prompt_version,
                    "remote": False,
                },
            )

        def close(self):
            pass

    def clock():
        return seed.now + timedelta(days=2)

    def service(_):
        return DailyInsightService(
            lambda: SqliteUnitOfWork(seed.database),
            None,
            now=clock,
            daily_analyzer=Analyzer(),
        )

    worker = DailyGenerationCoordinator(seed.database, service, "version", now=clock)
    try:
        assert worker.catch_up("startup")["created_job_ids"]
        result = worker.run_once()
        assert result["generation_completed"] and result["publish_completed"]
        assert worker.inventory.scan()[0]["classification"] == "CURRENT_COMPLETE"
        worker.catch_up("receiver_sync")
        assert worker.run_once() is None
        with seed.database.read() as c:
            assert (
                c.execute(
                    "SELECT count(*) FROM change_events WHERE resource_type='daily_summary'"
                ).fetchone()[0]
                == 1
            )
        before = worker.inventory.scan()[0]["source_fingerprint"]
        with seed.database.transaction() as c:
            c.execute(
                "UPDATE utterances SET revision=revision+1,updated_at=?",
                (clock().isoformat(),),
            )
        revised = worker.inventory.scan()[0]
        assert (
            revised["source_fingerprint"] != before
            and revised["classification"] == "STALE_SOURCE"
        )
    finally:
        seed.tearDown()


def test_provider_outage_stops_without_marking_remaining_failed(queue, tmp_path):
    q, _ = queue
    dates = [item(day=f"2026-01-0{i}") for i in range(1, 5)]
    worker = SimpleNamespace(inventory=SimpleNamespace(scan=lambda: dates))
    calls = []
    worker.run_once = lambda **_: (
        calls.append("run")
        or {
            "generation_completed": False,
            "first_request_infrastructure_failure": "capacity",
        }
    )
    result = DailyBackfillPass(
        worker, q.freeze_manifest(dates), tmp_path / "results.json"
    ).run()
    assert len(calls) == 3 and not result["BACKFILL_PASS_COMPLETE"]
    assert result["stop_reason"] == "PROVIDER_GLOBALLY_UNAVAILABLE"
    assert result["days"][-1]["status"] == "NOT_ATTEMPTED_PROVIDER_OUTAGE"


def test_provider_outage_hold_survives_receiver_restart(queue):
    q, _ = queue
    q.observe([item()], origin="HISTORICAL")
    q.hold_provider_outage([item()])
    q.observe([item()])
    assert q.states()[0]["status"] == "WAITING_PROVIDER"
    assert q.states()[0]["attempts"] == 0
    assert q.claim([item()]) is None
    q.retry(item()["date"], "approved-version")
    assert q.claim([item()])["attempts"] == 1


def test_backfill_journal_cross_process_exclusion(queue, tmp_path):
    q, _ = queue
    worker = SimpleNamespace(inventory=SimpleNamespace(scan=lambda: [item()]))
    worker.run_once = lambda **_: pytest.fail("Must not start a concurrent invocation")
    path = tmp_path / "results.json"
    runner = DailyBackfillPass(worker, q.freeze_manifest([item()]), path)
    with day_worker_lock(path) as acquired:
        assert acquired
        with pytest.raises(RuntimeError, match="journal is owned"):
            runner.run()
    assert not path.exists()
