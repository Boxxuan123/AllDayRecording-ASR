"""One SDK attempt, private receipts, and an exclusive 180-second process bound."""

import hashlib
import json
import time
import sys
from pathlib import Path
from uuid import uuid4

from allday_asr.build_info import runtime_build
from allday_asr.v3.application.generation_context import current_generation


class RemoteStartLimitReached(RuntimeError):
    pass


class RemoteBudget:
    def __init__(self, limit=160):
        self.limit = min(limit, 160)
        self.starts = 0

    def reserve(self):
        if self.starts >= self.limit:
            raise RemoteStartLimitReached("REMOTE_START_SAFETY_LIMIT_REACHED")
        self.starts += 1


def run_attempt(
    analyzer, request, instructions, schema, prompt_version, schema_version
):
    if sys.platform != "win32":
        raise RuntimeError(
            "SAFE_TIMEOUT_CONTROL_UNAVAILABLE: exclusive Windows Job required"
        )
    from .owned_attempt import OwnedAttempt

    budget = analyzer.remote_budget
    if budget is None:
        budget = analyzer.remote_budget = RemoteBudget()
    directory = (
        analyzer.receipt_dir or analyzer._workdir.parent / "daily-generation-receipts"
    )
    directory.mkdir(parents=True, exist_ok=True)
    stem = directory / uuid4().hex
    request_path = stem.with_suffix(".request.json")
    result_path = stem.with_suffix(".result.json")
    request_path.write_text(
        json.dumps(
            {
                "build": runtime_build(),
                "request": request,
                "instructions": instructions,
                "schema": schema,
                "prompt_version": prompt_version,
                "schema_version": schema_version,
                "model": analyzer.model_label,
                "workdir": str(analyzer._workdir),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    budget.reserve()
    analyzer.remote_starts += 1
    owned = OwnedAttempt(
        [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            __name__,
            str(request_path),
            str(result_path),
        ],
        stem.with_suffix(".log"),
        Path.cwd(),
    )
    context = current_generation.get()
    timeout = min(180, max(1, int(context.deadline-time.time()))) if context else 180
    status = owned.wait(timeout, context.cancelled if context else None)
    stem.with_suffix(".process.json").write_text(
        json.dumps(owned.identity, indent=2), encoding="utf-8"
    )
    if not result_path.exists():
        result_path.write_text(
            json.dumps(
                {
                    "status": "TIMEOUT" if status == 124 else "ERROR",
                    "error": "worker did not return",
                    "usage": None,
                }
            ),
            encoding="utf-8",
        )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if status == 125:
        raise InterruptedError("bounded daily model call cancelled")
    if status == 124:
        raise TimeoutError("bounded daily model attempt exceeded 180 seconds")
    if result["status"] != "RETURNED":
        message = result.get("error", "bounded worker failure")
        if result.get("error_type") in ("ValueError", "JSONDecodeError"):
            raise ValueError(message)
        raise RuntimeError(message)
    return result["payload"], {**result["provenance"], "receipt_id": stem.name,
        "request_ref": str(request_path.resolve()),
        "request_sha256": hashlib.sha256(request_path.read_bytes()).hexdigest()}


def worker(request_path, result_path):
    import os
    import traceback
    from openai_codex import Codex
    from .daily_semantic_analyzer import CodexDailySemanticAnalyzer
    from .event_generator import _usage
    from .owned_attempt import created, HANDLE

    data = json.loads(request_path.read_text(encoding="utf-8"))

    class RecordingClient(Codex):
        def __init__(self):
            super().__init__()
            proc = self._client._proc
            result_path.with_suffix(".sdk-process.json").write_text(
                json.dumps(
                    {
                        "pid": proc.pid,
                        "parent_pid": os.getpid(),
                        "creation_filetime": created(HANDLE(int(proc._handle))),
                        "exclusive_inherited_job": True,
                    }
                ),
                encoding="utf-8",
            )

        def thread_start(self, **kwargs):
            thread = super().thread_start(**kwargs)

            class RecordingThread:
                def run(self, *args, **options):
                    turn = thread.run(*args, **options)
                    result_path.with_suffix(".raw.json").write_text(
                        json.dumps(
                            {
                                "turn_id": str(turn.id),
                                "final_response": turn.final_response,
                                "usage": _usage(turn.usage),
                                "status": str(turn.status),
                                "items": [
                                    i.model_dump(mode="json", by_alias=True)
                                    for i in turn.items
                                ],
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                    return turn

            return RecordingThread()

    analyzer = CodexDailySemanticAnalyzer(
        Path(data["workdir"]), model=data["model"], codex_factory=RecordingClient
    )
    try:
        payload, provenance = analyzer._run_direct(
            data["request"],
            data["instructions"],
            data["schema"],
            data["prompt_version"],
            data["schema_version"],
        )
        result = {"status": "RETURNED", "payload": payload, "provenance": provenance}
    except BaseException as exc:
        result = {
            "status": "ERROR",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "traceback": traceback.format_exc(),
            "usage": None,
        }
    finally:
        result_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        analyzer.close()


if __name__ == "__main__":
    worker(Path(sys.argv[1]), Path(sys.argv[2]))
