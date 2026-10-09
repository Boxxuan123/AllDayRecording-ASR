"""Fixed synthetic data and temporary databases; no audio model or production state."""
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from allday_asr.v3.adapters.sqlite.runtime_diagnostics import RuntimeDiagnosticsRepository
from allday_asr.v3.adapters.sqlite.result_provenance import ResultProvenanceRepository
from allday_asr.v3.adapters.sqlite import V3Database
from allday_asr.v3.adapters.transfer import V3AutomaticWorkflowRunner, AutomaticWorkflowStateStore
from allday_asr.v3.application.event_extraction import SemanticEventExtractionService
from allday_asr.v3.application.generation_context import GenerationContext, generation_context
from allday_asr.v3.application.scoped_recompute import ScopedRecomputePass
from allday_asr.v3.interfaces.device_gateway import DeviceGateway
from tests import test_v32_semantic_events as event_fixture
from tests import test_v36_daily_insights as insight_fixture
from tests.test_v32_semantic_events import SESSION_ID, _FakeSemanticEventGenerator, _decision
from tests.test_model_execution_runner import call, successful_result
from allday_asr.v3.adapters.codex.model_execution_runner import ModelExecutionRunner


def test_ingest_ack_survives_postprocessing_enqueue_failure():
    verified = {"status": "ingested", "session_id": "durable-session", "input_revision": 1}
    ingest = Mock()
    ingest.ingest_completed.side_effect = [verified, {**verified, "status": "already_ingested"}]
    enqueue = Mock(side_effect=OSError("synthetic queue unavailable"))
    gateway = DeviceGateway(Mock(), Mock(), ingest, session_ingested=enqueue)
    first = gateway.upload_completed("phone-key", Mock(), Mock())
    second = gateway.upload_completed("phone-key", Mock(), Mock())
    assert first["status"] == "ingested" and second["status"] == "already_ingested"
    assert first["automation"]["status"] == "queue_failed"
    assert first["session_id"] == second["session_id"]


def test_post_stages_restart_skips_success_and_retries_only_failed_stage(tmp_path):
    session = "01ABCDEF012345678901234567"
    state = AutomaticWorkflowStateStore(tmp_path)
    state.initialize()
    state.save(session, {"version": 1, "session_id": session, "input_revision": 1})
    def runner():
        value = object.__new__(V3AutomaticWorkflowRunner)
        value._states = AutomaticWorkflowStateStore(tmp_path)
        value._require_input_revision = lambda sid, rev: None
        return value
    first = runner()
    identity = Mock(return_value={"product_inference_executed": True})
    event = Mock(side_effect=RuntimeError("synthetic event failure"))
    first._post_stage(session, 1, "speaker_identity", identity)
    with pytest.raises(RuntimeError):
        first._post_stage(session, 1, "events", event)
    resumed = runner()
    assert resumed._post_stage(session, 1, "speaker_identity", identity)["product_inference_executed"]
    event.side_effect = None
    event.return_value = {"generation_id": "one"}
    resumed._post_stage(session, 1, "events", event)
    resumed._post_stage(session, 1, "events", event)
    assert identity.call_count == 1 and event.call_count == 2
    # A genuine changed input revision may execute the stage again.
    resumed._post_stage(session, 2, "speaker_identity", identity)
    assert identity.call_count == 2


def test_reported_versions_are_independent_and_historical_rows_stay_unknown(tmp_path):
    database = V3Database.open(tmp_path / "core.sqlite3")
    repository = RuntimeDiagnosticsRepository(database)
    assert repository.snapshot()["devices"] == {}
    phone = {"component": "phone", "release_version": "1.0.2", "git_commit": "a"*40, "dirty": False}
    watch = {"component": "watch", "release_version": "1.0.1", "git_commit": "b"*40, "dirty": False}
    repository.report("actual-phone", {"phone": phone, "watch": {
        "build": watch, "last_seen": time.time()*1000, "state": "reported"}})
    result = repository.snapshot()
    assert result["devices"]["actual-phone"]["phone"] == phone
    assert result["devices"]["actual-phone"]["watch"]["build"] == watch
    assert result["build"]["release_version"] != phone["release_version"]
    # Add no fictitious report for an old or offline device.
    assert result["unreported_devices"] == "unknown"


def test_scoped_event_recompute_reuses_transcript_updates_only_requested_event(tmp_path):
    fixture = event_fixture.V32SemanticEventTests()
    fixture.setUp()
    try:
        first_generator = _FakeSemanticEventGenerator((_decision(),))
        service = SemanticEventExtractionService(fixture.factory, fixture.knowledge, first_generator)
        first = service.extract(SESSION_ID)
        event_id = first["semantic_events"][0]["event_id"]
        second_generator = _FakeSemanticEventGenerator(({**_decision(), "summary": "explicit revised rule result"},))
        second_generator.prompt_version = "sample-rule-revision-2"
        service._generator = second_generator
        with fixture.database.read() as c:
            before = tuple(c.execute("SELECT utterance_id,run_id,revision,text FROM utterances").fetchall())
            runs_before = c.execute("SELECT COUNT(*) FROM processing_runs").fetchone()[0]
        core = SimpleNamespace(semantic_events=service)
        job = ScopedRecomputePass(core, tmp_path, execution_id="sample-event-1",
                                  stage="events", targets=[SESSION_ID])
        result = job.run()
        assert result["status"] == "succeeded", result
        event = fixture.knowledge.list_events(SESSION_ID)[0]
        assert event["event_id"] == event_id and event["revision"] == 2
        assert event["payload"]["summary"] == "explicit revised rule result"
        replay = job.run()
        assert replay == result
        assert fixture.knowledge.list_events(SESSION_ID)[0]["revision"] == 2
        with fixture.database.read() as c:
            assert tuple(c.execute("SELECT utterance_id,run_id,revision,text FROM utterances").fetchall()) == before
            assert c.execute("SELECT COUNT(*) FROM processing_runs").fetchone()[0] == runs_before
        generation_id = result["items"][0]["result"]["generation_id"]
        source = ResultProvenanceRepository(fixture.database).get("generation", generation_id)
        assert source["status"] == "recorded"
        assert source["provenance"]["inputs"]["utterances"][0]["revision"] == 1
        assert source["provenance"]["build"]["git_commit"]
        assert source["existing_source_data"]["input_snapshot"]["kind"] in {"event-request", "semantic-event"}
    finally:
        fixture.tearDown()


def test_scoped_summary_uses_events_and_replays_without_another_revision(tmp_path):
    fixture = insight_fixture.V36DailyInsightTests()
    fixture.setUp()
    try:
        core = SimpleNamespace(insights=SimpleNamespace(generate_daily=fixture.insights.generate_daily))
        job = ScopedRecomputePass(core, tmp_path, execution_id="one-summary",
                                  stage="summary", targets=["2026-09-01"])
        result = job.run()
        assert result["status"] == "succeeded", result
        assert job.run() == result
        assert len(fixture.generator.daily_requests) == 1
        summary = fixture.insights.daily("2026-09-01", "Asia/Singapore")
        provenance = ResultProvenanceRepository(fixture.database).get("generation", summary["generation_id"])
        assert provenance["provenance"]["inputs"]["source_events"]
        assert provenance["provenance"]["execution_id"] == "one-summary"
    finally:
        fixture.tearDown()


def test_scoped_limits_cancel_deadline_and_retry_are_durable(tmp_path):
    action = Mock(side_effect=RuntimeError("synthetic bounded failure"))
    core = SimpleNamespace(semantic_events=SimpleNamespace(extract=action))
    with pytest.raises(ValueError, match="1-8"):
        ScopedRecomputePass(core, tmp_path, execution_id="too-many", stage="events",
                            targets=[str(i) for i in range(9)])
    job = ScopedRecomputePass(core, tmp_path, execution_id="failed", stage="events", targets=["one"])
    assert job.run()["status"] == "failed"
    assert job.run()["status"] == "failed"
    assert job.run()["status"] == "failed"
    assert action.call_count == 2
    cancelled = ScopedRecomputePass(core, tmp_path, execution_id="cancel", stage="events", targets=["two"])
    cancelled.cancel()
    assert cancelled.run()["status"] == "cancelled"
    expired = ScopedRecomputePass(core, tmp_path, execution_id="expired", stage="events", targets=["three"])
    expired.path.write_text(json.dumps({"scope": expired.scope, "status": "queued",
        "deadline": time.time()-1, "items": [{"target": "three", "attempts": 0, "status": "queued"}]}))
    assert expired.run()["status"] == "timeout"
    assert action.call_count == 2


def test_build_change_does_not_invalidate_completed_model_job(tmp_path):
    runner = ModelExecutionRunner(tmp_path / "receipts")
    with patch.object(runner, "_owned_call", return_value=successful_result()) as model:
        with patch("allday_asr.build_info.runtime_build", return_value={"git_commit": "first"}):
            first = call(runner)
        with patch("allday_asr.build_info.runtime_build", return_value={"git_commit": "second"}):
            second = call(runner)
        assert first.id == second.id and model.call_count == 1
    receipt = json.loads(next((tmp_path / "receipts").glob("*.request.snapshot.json")).read_text("utf-8"))
    assert receipt["request"]["model"] == "fake"
    assert receipt["build"]["git_commit"] == "first"


def test_source_changes_during_generation_cannot_be_published():
    fixture = event_fixture.V32SemanticEventTests()
    fixture.setUp()
    try:
        generator = _FakeSemanticEventGenerator((_decision(),))
        original = generator.generate
        def change_source(*args):
            result = original(*args)
            with fixture.database.transaction() as c:
                c.execute("UPDATE utterances SET revision=revision+1,text='changed' WHERE session_id=?", (SESSION_ID,))
            return result
        generator.generate = change_source
        service = SemanticEventExtractionService(fixture.factory, fixture.knowledge, generator)
        with generation_context(GenerationContext("one", time.time()+60)):
            with pytest.raises(ValueError, match="snapshot changed|evidence or context changed"):
                service.extract(SESSION_ID, recompute=True)
        assert fixture.knowledge.list_events(SESSION_ID) == ()
    finally:
        fixture.tearDown()


def test_real_daily_summary_only_uses_existing_events(tmp_path):
    from tests import test_v32_three_layer_knowledge as seed_module
    from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
    from allday_asr.v3.application.insights import DailyInsightService
    seed = seed_module.V32ThreeLayerKnowledgeTests()
    seed.setUp()
    try:
        service = DailyInsightService(lambda: SqliteUnitOfWork(seed.database), None, now=lambda: seed.now)
        original = service.refresh_daily("2026-09-01")
        with seed.database.read() as c:
            events = tuple(c.execute("SELECT event_id, revision FROM event_current_states").fetchall())
            utterances = tuple(c.execute("SELECT utterance_id,revision,text FROM utterances").fetchall())
        service.refresh_daily = Mock(side_effect=AssertionError("must not re-extract events"))
        core = SimpleNamespace(insights=service)
        job = ScopedRecomputePass(core, tmp_path, execution_id="actual-daily", stage="summary", targets=["2026-09-01"])
        result = job.run()
        assert result["status"] == "succeeded", result
        assert result["items"][0]["result"]["objective"]["source_event_ids"] == original["objective"]["source_event_ids"]
        assert job.run() == result
        with seed.database.read() as c:
            assert tuple(c.execute("SELECT event_id,revision FROM event_current_states").fetchall()) == events
            assert tuple(c.execute("SELECT utterance_id,revision,text FROM utterances").fetchall()) == utterances
        service.refresh_daily.assert_not_called()
    finally:
        seed.tearDown()


def test_historical_generation_does_not_fabricate_source():
    seed = event_fixture.V32SemanticEventTests()
    seed.setUp()
    try:
        receipt = SemanticEventExtractionService(seed.factory, seed.knowledge,
            _FakeSemanticEventGenerator((_decision(),))).extract(SESSION_ID)
        with seed.database.transaction() as c:
            c.execute("UPDATE generation_records SET input_scope_json='{}' WHERE generation_id=?", (receipt["generation_id"],))
        source = ResultProvenanceRepository(seed.database).get("generation", receipt["generation_id"])
        assert source["status"] == "historical_unknown" and source["provenance"] is None
    finally:
        seed.tearDown()


def test_configuration_upgrade_does_not_enqueue_historical_sources():
    from tests import test_daily_inventory_completion as inventory_fixture
    from allday_asr.v3.adapters.sqlite.daily_inventory import DailyHistoryInventory
    from allday_asr.v3.application.daily_automation import DailyGenerationCoordinator
    fixture = inventory_fixture.publication.__wrapped__()
    state = next(fixture)
    try:
        inventory_fixture.publish(state)
        inventory_fixture.job(state)
        upgraded = DailyHistoryInventory(state.seed.database, "explicit-new-prompt-rule", now=state.now)
        item = next(i for i in upgraded.scan() if i["date"] == state.day)
        assert item["automatic_recompute_blocked"]
        forbidden = Mock(side_effect=AssertionError("upgrade must not replay history"))
        coordinator = DailyGenerationCoordinator(
            state.seed.database, forbidden, "explicit-new-prompt-rule", now=state.now)
        coordinator.inventory = upgraded
        before = coordinator.queue.states()
        assert coordinator.catch_up("startup")["created_job_ids"] == []
        assert coordinator.run_once() is None
        assert coordinator.queue.states() == before
        forbidden.assert_not_called()
        # A genuine evidence revision still follows the established automatic rule.
        with state.seed.database.transaction() as c:
            c.execute("UPDATE utterances SET revision=revision+1")
        changed = next(i for i in upgraded.scan() if i["date"] == state.day)
        assert not changed["automatic_recompute_blocked"]
    finally:
        try:
            next(fixture)
        except StopIteration:
            pass


def test_actual_signed_diagnostic_report_is_bound_once_and_accepts_unequal_releases(tmp_path):
    import threading
    import urllib.request
    import urllib.error
    from tests import test_v3_device_sync as device_fixture
    from allday_asr.v3.interfaces.transfer.server import create_transfer_server
    from allday_asr.v3.interfaces.transfer.passkeys import RequestBinding
    from allday_asr.v3.interfaces.transfer.devices import build_device_signature_payload
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    fixture = device_fixture.V3DeviceSyncTests()
    db, key, record, trust, _, service = fixture._environment(tmp_path)
    gateway = DeviceGateway(trust, service, review_service=SimpleNamespace(core=SimpleNamespace(database=db)))
    server = create_transfer_server(inbox=tmp_path/"inbox", host="127.0.0.1", port=0,
        token="synthetic-pairing-code", allow_insecure_http=True, device_manager=trust,
        receiver_id=device_fixture.RECEIVER_ID, v3_gateway=gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.port}"
    payload = {"phone": {"component": "phone", "release_version": "9.9.0",
        "git_commit": "d"*40, "dirty": False}, "watch": {"state": "unknown", "last_seen": 0}}
    raw = json.dumps(payload).encode()
    binding = RequestBinding.for_request(method="POST", path="/device/v3/diagnostics", body=raw)
    try:
        request = urllib.request.Request(base+"/api/v1/devices/authenticate/challenge",
            data=json.dumps({"device_id": record.device_id, "request": {
                "method": binding.method, "path": binding.path,
                "body_sha256": binding.body_sha256, "upload_offset": binding.upload_offset}}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=3) as response:
            challenge = json.loads(response.read())
        signature = key.sign(build_device_signature_payload(challenge_id=challenge["challenge_id"],
            nonce=challenge["nonce"], binding=binding), ec.ECDSA(hashes.SHA256()))
        report = urllib.request.Request(base+"/device/v3/diagnostics", data=raw, method="POST",
            headers={"Content-Type": "application/json", "X-AllDay-Device-ID": record.device_id,
                "X-AllDay-Device-Challenge": challenge["challenge_id"],
                "X-AllDay-Device-Signature": device_fixture._b64(signature)})
        with urllib.request.urlopen(report, timeout=3) as response:
            assert json.loads(response.read())["status"] == "recorded"
        with pytest.raises(urllib.error.HTTPError) as replay:
            urllib.request.urlopen(report, timeout=3)
        assert replay.value.code == 401
        snapshot = RuntimeDiagnosticsRepository(db).snapshot()
        actual = snapshot["devices"][trust.domain_device_id(record.device_id)]
        assert actual["phone"]["release_version"] == "9.9.0"
        assert actual["watch"]["state"] == "unknown"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
