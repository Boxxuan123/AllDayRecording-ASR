"""Historical tools must preserve frozen evidence and avoid unrelated jobs."""

import hashlib
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from tools import benchmark_speaker_recognition_v2 as benchmark
from tools import finalize_repaired_result as publication
from tools import run_repair_processing as processing
from tests import test_v3_durable_processing as fixtures
from allday_asr.v3.application import DurableProcessingWorker


def test_benchmark_refuses_existing_output_and_preserves_settings(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(benchmark, "ROOT", tmp_path)
    output = tmp_path / "outputs" / "historical"
    output.mkdir(parents=True)
    frozen = output / "frozen-settings.json"
    frozen.write_text('{"historical":true}')
    with pytest.raises(FileExistsError):
        benchmark.main(["--output", str(output)])
    assert frozen.read_text() == '{"historical":true}'
    with pytest.raises(ValueError, match="private ignored"):
        benchmark.prepare_output(tmp_path / "docs" / "results")


def test_benchmark_fits_only_validation_not_test(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmark, "OUT", tmp_path)
    refs = {"a": [np.array([1.0, 0.0])], "b": [np.array([0.0, 1.0])]}

    def query(truth, vector):
        return {
            "truth": truth,
            "vector": np.array(vector),
            "duration_s": 8.0,
            "quality": 1.0,
        }

    validation = [query("a", [1.0, 0.0]), query("unknown", [0.7, 0.7])]
    first = benchmark.evaluate_case(
        "first", validation, [query("a", [1.0, 0.0])], refs, {"a", "b"}
    )
    second = benchmark.evaluate_case(
        "second", validation, [query("b", [1.0, 0.0])], refs, {"a", "b"}
    )
    assert first["settings"] == second["settings"]
    assert first["test"]["correct"] == 1
    assert second["test"]["wrong_person"] == 1


def test_append_preconditions_still_execute_under_optimized_python():
    code = "from tools.repair_phone_session_append import require; require(False, 'invalid recovery input')"
    result = subprocess.run(
        [sys.executable, "-O", "-c", code], capture_output=True, text=True
    )
    assert result.returncode != 0 and "invalid recovery input" in result.stderr


@pytest.mark.parametrize("module", [processing, publication])
def test_processing_and_publication_default_to_preview(module, monkeypatch):
    core = MagicMock()
    snapshot = SimpleNamespace(
        job=SimpleNamespace(status="queued" if module is processing else "succeeded"),
        run=SimpleNamespace(session_id=module.SESSION_ID, input_revision=2),
    )
    core.processing.get.return_value = snapshot
    core.processing.get_for_run.return_value = snapshot
    monkeypatch.setattr(module, "compose_v3_core", lambda *args: core)
    module.main([])
    core.initialize.assert_not_called()
    core.database.transaction.assert_not_called()
    core.close.assert_called_once()


def test_processing_rejects_wrong_session_before_any_write(monkeypatch):
    core = MagicMock()
    core.processing.get.return_value = SimpleNamespace(
        job=SimpleNamespace(status="queued"),
        run=SimpleNamespace(session_id="unrelated-session", input_revision=2),
    )
    monkeypatch.setattr(processing, "compose_v3_core", lambda: core)
    with pytest.raises(ValueError, match="expected recovered input"):
        processing.main(["--apply"])
    core.initialize.assert_not_called()


@pytest.fixture
def durable():
    helper = fixtures.V3DurableProcessingTests()
    helper.setUp()
    try:
        yield helper
    finally:
        helper.tearDown()


def ledger(database):
    with database.read() as c:
        tables = [
            r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        return {
            t: sorted(
                [tuple(r) for r in c.execute('SELECT * FROM "' + t + '"')], key=str
            )
            for t in tables
        }


def test_mismatched_repair_claim_rolls_back_all_database_writes(durable):
    actual = durable.service.submit(durable._command())
    before = ledger(durable.database)
    guard = processing.TargetOnlyService(durable.service, "desired-repair-job")
    with pytest.raises(RuntimeError, match="unrelated queue work"):
        guard.claim("repair", {})
    assert durable.service.get(actual.job.job_id).job.status == "queued"
    assert ledger(durable.database) == before
    assert guard.recover_expired() == ()


def test_target_job_runs_normally_with_native_worker_contract(durable):
    target = durable.service.submit(durable._command())
    guard = processing.TargetOnlyService(durable.service, target.job.job_id)
    worker = DurableProcessingWorker(
        guard, fixtures._SuccessfulAdapter(), worker_id="targeted-test"
    )
    for _ in target.stages:
        assert worker.run_once()
    assert durable.service.get(target.job.job_id).job.status == "succeeded"


def test_human_manifest_excludes_automatic_only_truth(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setattr(benchmark, "OLD", tmp_path)
    (tmp_path / "manifest.json").write_text("{}")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        "CREATE TABLE annotation_facts(fact_id TEXT,actor TEXT,value_json TEXT,state TEXT,dimension TEXT); CREATE TABLE annotation_fact_audio(fact_id TEXT,media_id TEXT,start_ms INTEGER,end_ms INTEGER);"
    )
    c.execute(
        "INSERT INTO annotation_facts VALUES('auto','system:speaker-cluster-identity',?,'active','person')",
        (json.dumps("a"),),
    )
    c.execute("INSERT INTO annotation_fact_audio VALUES('auto','media',0,2000)")
    w = {
        "id": "window",
        "person_id": "a",
        "media_id": "media",
        "start_ms": 0,
        "end_ms": 2000,
    }
    original = {"windows": [w], "groups": []}
    kept, excluded = benchmark.human_only_manifest(original, c)
    assert kept["windows"] == [] and excluded == [w]
    c.execute(
        "INSERT INTO annotation_facts VALUES('human','test-human',?,'active','person')",
        (json.dumps("a"),),
    )
    c.execute("INSERT INTO annotation_fact_audio VALUES('human','media',0,2000)")
    kept, excluded = benchmark.human_only_manifest(original, c)
    assert kept["windows"] == [w] and excluded == []
    assert kept["old_manifest_sha256"] == hashlib.sha256(b"{}").hexdigest()
    c.close()
