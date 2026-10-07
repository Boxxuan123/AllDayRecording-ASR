"""Synthetic model failures; no remote model is started."""

import json
import sys
from pathlib import Path
from threading import Event, Timer
from unittest.mock import patch

import pytest

from allday_asr.v3.adapters.codex.model_execution_runner import (
    ModelExecutionCancelled,
    ModelExecutionRunner,
    ModelExecutionTerminal,
)


def call(runner, **overrides):
    values = dict(task="synthetic", batch_id="one-batch", prompt="{}",
                  instructions="local fixture", schema={"type": "object"},
                  model="fake", effort="low", workdir=runner.receipt_dir.parent)
    values.update(overrides)
    return runner.run(**values)


def successful_result():
    return {"id": "synthetic-turn", "final_response": "{}",
            "usage": {"input_tokens": 1, "output_tokens": 1}, "items": []}


def test_timeouts_exhaust_persisted_job_budget(tmp_path):
    runner = ModelExecutionRunner(tmp_path / "receipts", max_attempts=2)
    with patch.object(runner, "_owned_call", side_effect=TimeoutError("injected")):
        with pytest.raises(TimeoutError):
            call(runner)
        with pytest.raises(TimeoutError):
            call(runner)
    restarted = ModelExecutionRunner(tmp_path / "receipts", max_attempts=2)
    with pytest.raises(ModelExecutionTerminal, match="retry budget"):
        call(restarted)
    receipts = sorted((tmp_path / "receipts").glob("*.receipt.json"))
    assert [json.loads(path.read_text())["status"] for path in receipts] == [
        "timeout", "timeout"]


def test_cancelled_call_has_receipt_and_never_starts_remote(tmp_path):
    runner = ModelExecutionRunner(tmp_path / "receipts")
    cancelled = Event()
    cancelled.set()
    with patch.object(runner, "_owned_call") as remote:
        with pytest.raises(ModelExecutionCancelled):
            call(runner, cancel_event=cancelled)
        remote.assert_not_called()
    receipt = next((tmp_path / "receipts").glob("*.receipt.json"))
    assert json.loads(receipt.read_text())["status"] == "cancelled"
    with pytest.raises(ModelExecutionTerminal, match="retry budget"):
        call(ModelExecutionRunner(tmp_path / "receipts"))


def test_batch_call_budget_survives_restart(tmp_path):
    runner = ModelExecutionRunner(tmp_path / "receipts", max_batch_calls=1)
    with patch.object(runner, "_owned_call", return_value=successful_result()):
        assert call(runner).id == "synthetic-turn"
    restarted = ModelExecutionRunner(tmp_path / "receipts", max_batch_calls=100)
    with pytest.raises(ModelExecutionTerminal, match="batch budget"):
        call(restarted, prompt='{"different":true}')
    batches = list((tmp_path / "receipts").glob("batch-*.json"))
    assert len(batches) == 1
    assert json.loads(batches[0].read_text())["calls"] == 1


def test_reported_token_budget_is_terminal(tmp_path):
    runner = ModelExecutionRunner(tmp_path / "receipts", max_job_tokens=1)
    with patch.object(runner, "_owned_call", return_value=successful_result()):
        with pytest.raises(ModelExecutionTerminal, match="token budget"):
            call(runner)
    receipt = next((tmp_path / "receipts").glob("*.receipt.json"))
    assert json.loads(receipt.read_text())["usage"]["input_tokens"] == 1
    with pytest.raises(ModelExecutionTerminal):
        call(ModelExecutionRunner(tmp_path / "receipts", max_job_tokens=10))


def test_restart_can_retry_started_attempt_once(tmp_path):
    runner = ModelExecutionRunner(tmp_path / "receipts", max_attempts=2)
    with patch.object(runner, "_owned_call", side_effect=TimeoutError("injected")):
        with pytest.raises(TimeoutError):
            call(runner)
    state_path = next((tmp_path / "receipts").glob("*.job.json"))
    state = json.loads(state_path.read_text())
    state["status"] = "started"  # Synthetic death after recording the first attempt.
    state_path.write_text(json.dumps(state))
    first_receipt = next((tmp_path / "receipts").glob("*.receipt.json"))
    receipt = json.loads(first_receipt.read_text())
    receipt["status"] = "started"
    first_receipt.write_text(json.dumps(receipt))
    restarted = ModelExecutionRunner(tmp_path / "receipts", max_attempts=2)
    with patch.object(restarted, "_owned_call", return_value=successful_result()):
        assert call(restarted).id == "synthetic-turn"
    recovered = json.loads(first_receipt.read_text())
    assert (recovered["status"], recovered["error_type"], recovered["retryable"]) == (
        "failed", "ProcessRestart", True)
    assert json.loads(state_path.read_text())["attempts"] == 2
    assert call(ModelExecutionRunner(tmp_path / "receipts")).id == "synthetic-turn"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows exclusive Job only")
def test_owned_child_process_is_cancelled(tmp_path):
    from allday_asr.v3.adapters.codex.owned_attempt import OwnedAttempt

    owned = OwnedAttempt(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        tmp_path / "child.log", Path.cwd(),
    )
    cancellation = Event()
    timer = Timer(0.1, cancellation.set)
    timer.start()
    try:
        assert owned.wait(5, cancellation) == 125
    finally:
        timer.cancel()
