"""One bounded model call with durable per-job attempts and private receipts.

The production SDK call runs in an exclusively owned Windows Job. A caller may
retry the same job explicitly, up to the persisted attempt limit. No retry or
next batch is started by this runner.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from threading import Event, RLock
from types import SimpleNamespace
from typing import Any

from allday_asr.v3.application.file_lock import day_worker_lock


ALLOWED_ITEMS = {"agentMessage", "contextCompaction", "plan", "reasoning", "userMessage"}


class ModelExecutionTerminal(RuntimeError):
    pass


class ModelExecutionCancelled(RuntimeError):
    pass


def _atomic_json(path: Path, value: dict) -> None:
    part = path.with_suffix(path.suffix + ".part")
    with part.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    part.replace(path)


def _result_data(result: Any) -> dict[str, Any]:
    items = []
    for item in result.items:
        value = getattr(item, "root", item)
        kind = getattr(value, "type", None)
        if kind not in ALLOWED_ITEMS:
            raise ValueError(f"model produced tool or unknown activity: {kind}")
        items.append(kind)
    usage = result.usage
    if usage is not None and hasattr(usage, "model_dump"):
        usage = usage.model_dump(by_alias=True, mode="json", exclude_none=True)
    if not isinstance(usage, (dict, type(None))):
        usage = None
    if not isinstance(result.final_response, str):
        raise ValueError("model returned no final response")
    json.loads(result.final_response)
    return {"id": str(result.id), "final_response": result.final_response,
            "usage": usage, "items": items}


def _restore_result(data: dict[str, Any]) -> Any:
    return SimpleNamespace(
        id=data["id"], final_response=data["final_response"],
        usage=data.get("usage"),
        items=[SimpleNamespace(type=kind) for kind in data["items"]],
    )


def _reported_tokens(usage: dict | None) -> int:
    if not usage:
        return 0
    total = usage.get("total_tokens", usage.get("totalTokens"))
    if isinstance(total, int) and total >= 0:
        return total
    inbound = usage.get("input_tokens", usage.get("inputTokens", 0))
    outbound = usage.get("output_tokens", usage.get("outputTokens", 0))
    return sum(value for value in (inbound, outbound)
               if isinstance(value, int) and value >= 0)


class ModelExecutionRunner:
    def __init__(self, receipt_dir: Path, *, timeout_seconds: int = 180,
                 max_attempts: int = 2, max_batch_calls: int = 16,
                 batch_deadline_seconds: int = 1800,
                 max_prompt_bytes: int = 256_000,
                 max_job_tokens: int = 150_000) -> None:
        if min(timeout_seconds, max_attempts, max_batch_calls,
               batch_deadline_seconds, max_prompt_bytes, max_job_tokens) <= 0:
            raise ValueError("model execution limits must be positive")
        self.receipt_dir = receipt_dir
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.max_batch_calls = max_batch_calls
        self.batch_deadline_seconds = batch_deadline_seconds
        self.max_prompt_bytes = max_prompt_bytes
        self.max_job_tokens = max_job_tokens
        self._lock = RLock()

    def run(self, *, task: str, batch_id: str, prompt: str,
            instructions: str, schema: dict, model: str | None,
            effort: str, workdir: Path, fake_client: Any | None = None,
            cancel_event: Event | None = None) -> Any:
        identity = {"task": task, "prompt": prompt, "instructions": instructions,
                    "schema": schema, "model": model, "effort": effort}
        if len(json.dumps(identity, ensure_ascii=False).encode("utf-8")) > self.max_prompt_bytes:
            raise ModelExecutionTerminal("model request budget exceeded")
        job_id = hashlib.sha256(json.dumps(identity, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        self.receipt_dir.mkdir(parents=True, exist_ok=True)
        state_path = self.receipt_dir / f"{job_id}.job.json"
        with self._lock, day_worker_lock(state_path) as acquired:
            if not acquired:
                raise RuntimeError("model job is already running")
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {
                "job_id": job_id, "task": task, "attempts": 0,
                "max_attempts": self.max_attempts, "status": "pending",
            }
            if state["status"] == "started":
                # The per-job lock is now ours, so the prior owner died. The
                # owned child was terminated with it; close the orphan receipt
                # before considering a bounded explicit retry.
                old_attempt = state["attempts"]
                prior_path = self.receipt_dir / f"{job_id}.{old_attempt}.receipt.json"
                prior = json.loads(prior_path.read_text(encoding="utf-8")) if prior_path.exists() else {
                    "job_id": job_id, "attempt": old_attempt,
                    "started_at": state.get("started_at"), "usage": None,
                }
                old_limit = min(self.max_attempts, state.get("max_attempts", self.max_attempts))
                prior.update(status="failed", completed_at=time.time(),
                             error_type="ProcessRestart", retryable=old_attempt < old_limit)
                state.update(status="retryable" if old_attempt < old_limit else "terminal",
                             completed_at=prior["completed_at"])
                _atomic_json(prior_path, prior)
                _atomic_json(state_path, state)
            if state["status"] == "completed":
                return _restore_result(state["result"])
            attempt_limit = min(self.max_attempts, state.get("max_attempts", self.max_attempts))
            if state["status"] == "terminal" or state["attempts"] >= attempt_limit:
                raise ModelExecutionTerminal("model job retry budget exhausted")
            if state.get("used_tokens", 0) >= min(self.max_job_tokens,
                                                  state.get("max_job_tokens", self.max_job_tokens)):
                raise ModelExecutionTerminal("model job token budget exhausted")
            batch_key = hashlib.sha256(batch_id.encode("utf-8")).hexdigest()
            batch_path = self.receipt_dir / f"batch-{batch_key}.json"
            with day_worker_lock(batch_path) as batch_acquired:
                if not batch_acquired:
                    raise RuntimeError("model batch is already running")
                batch = json.loads(batch_path.read_text(encoding="utf-8")) if batch_path.exists() else {
                    "batch_id_hash": batch_key, "started_at": time.time(), "calls": 0,
                    "max_calls": self.max_batch_calls,
                    "deadline_at": time.time() + self.batch_deadline_seconds,
                }
                if (batch["calls"] >= min(self.max_batch_calls, batch.get("max_calls", self.max_batch_calls)) or
                    time.time() > min(batch.get("deadline_at", float("inf")),
                                      batch["started_at"] + self.batch_deadline_seconds)):
                    raise ModelExecutionTerminal("model batch budget exhausted")
                batch["calls"] += 1
                _atomic_json(batch_path, batch)
                batch_remaining = max(1, int(min(
                    batch.get("deadline_at", float("inf")),
                    batch["started_at"] + self.batch_deadline_seconds,
                ) - time.time()))
            attempt = state["attempts"] + 1
            state.setdefault("max_job_tokens", self.max_job_tokens)
            state.update(attempts=attempt, status="started", started_at=time.time())
            _atomic_json(state_path, state)
            receipt_path = self.receipt_dir / f"{job_id}.{attempt}.receipt.json"
            receipt = {"job_id": job_id, "attempt": attempt, "status": "started",
                       "started_at": state["started_at"], "usage": None}
            _atomic_json(receipt_path, receipt)
            try:
                if cancel_event is not None and cancel_event.is_set():
                    raise ModelExecutionCancelled("model call cancelled")
                if fake_client is not None:
                    raw = _direct_call(fake_client, prompt, instructions, schema,
                                       model, effort, workdir)
                    data = _result_data(raw)
                else:
                    data = self._owned_call(identity, workdir, job_id, attempt,
                                            cancel_event, min(self.timeout_seconds, batch_remaining))
                receipt["usage"] = data.get("usage")
                state["used_tokens"] = state.get("used_tokens", 0) + _reported_tokens(data.get("usage"))
                if state["used_tokens"] > min(self.max_job_tokens, state["max_job_tokens"]):
                    raise ModelExecutionTerminal("model job token budget exceeded")
                receipt.update(status="completed", completed_at=time.time(),
                               turn_id=data["id"])
                state.update(status="completed", completed_at=receipt["completed_at"],
                             result=data)
                _atomic_json(receipt_path, receipt)
                _atomic_json(state_path, state)
                return _restore_result(data)
            except BaseException as exc:
                status = ("cancelled" if isinstance(exc, ModelExecutionCancelled) else
                          "timeout" if isinstance(exc, TimeoutError) else "failed")
                terminal = attempt >= attempt_limit or status == "cancelled" or isinstance(exc, ModelExecutionTerminal)
                receipt.update(status=status, completed_at=time.time(),
                               error_type=type(exc).__name__,
                               retryable=not terminal)
                state.update(status="terminal" if terminal else "retryable",
                             completed_at=receipt["completed_at"])
                _atomic_json(receipt_path, receipt)
                _atomic_json(state_path, state)
                raise

    def _owned_call(self, identity: dict, workdir: Path, job_id: str,
                    attempt: int, cancel_event: Event | None,
                    timeout_seconds: int) -> dict:
        if sys.platform != "win32":
            raise RuntimeError("SAFE_TIMEOUT_CONTROL_UNAVAILABLE")
        from .owned_attempt import OwnedAttempt

        stem = self.receipt_dir / f"{job_id}.{attempt}"
        request_path = Path(f"{stem}.request.json")
        result_path = Path(f"{stem}.result.json")
        _atomic_json(request_path, {**identity, "workdir": str(workdir)})
        owned = OwnedAttempt(
            [sys.executable, "-X", "utf8", "-m", __name__,
             str(request_path), str(result_path)],
            Path(f"{stem}.log"), Path.cwd(),
        )
        exit_code = owned.wait(timeout_seconds, cancel_event)
        _atomic_json(Path(f"{stem}.process.json"), owned.identity)
        if exit_code == 124:
            raise TimeoutError("model call exceeded its deadline")
        if exit_code == 125:
            raise ModelExecutionCancelled("model call cancelled")
        if exit_code != 0 or not result_path.exists():
            raise RuntimeError("owned model worker failed")
        output = json.loads(result_path.read_text(encoding="utf-8"))
        if output["status"] != "completed":
            raise RuntimeError(output.get("error", "model worker failed"))
        return output["result"]


def _direct_call(client: Any, prompt: str, instructions: str, schema: dict,
                 model: str | None, effort: str, workdir: Path) -> Any:
    from openai_codex import ApprovalMode, Sandbox
    from openai_codex.types import ReasoningEffort

    thread = client.thread_start(
        approval_mode=ApprovalMode.deny_all, cwd=str(workdir),
        developer_instructions=instructions, ephemeral=True,
        model=model, sandbox=Sandbox.read_only,
    )
    return thread.run(
        prompt, approval_mode=ApprovalMode.deny_all, cwd=str(workdir),
        effort=ReasoningEffort(effort), model=model,
        output_schema=schema, sandbox=Sandbox.read_only,
    )


def worker(request_path: Path, result_path: Path) -> None:
    from openai_codex import Codex

    request = json.loads(request_path.read_text(encoding="utf-8"))
    client = Codex()
    try:
        raw = _direct_call(client, request["prompt"], request["instructions"],
                           request["schema"], request["model"],
                           request["effort"], Path(request["workdir"]))
        output = {"status": "completed", "result": _result_data(raw)}
    except BaseException as exc:
        output = {"status": "failed", "error": str(exc),
                  "error_type": type(exc).__name__}
    finally:
        client.close()
    _atomic_json(result_path, output)


if __name__ == "__main__":
    worker(Path(sys.argv[1]), Path(sys.argv[2]))
