"""Fixed exposed development comparison, evaluation-only, never imported by ingestion."""

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from allday_asr.v3.adapters.sqlite import V3Database
from allday_asr.v3.adapters.codex.model_execution_runner import ModelExecutionRunner
import allday_asr.v3.application.history_recall as history


def baseline(snapshot, case, root, shared_model):
    # Generic queryable source emulator: arbitrary scope/cursor/context queries
    # over all frozen native records. No question-to-answer lookup exists.
    from .frozen import FrozenSnapshot

    source = FrozenSnapshot(snapshot)
    source.dataset = snapshot["dataset_id"]
    folder = root / "baseline" / case["id"]
    folder.mkdir(parents=True, exist_ok=True)
    database = V3Database.open(folder / "core.sqlite3")
    # Load the unchanged composition file without bootstrap.__init__ importing ASR.
    import importlib.util

    composition_path = (
        Path(__file__).resolve().parents[1] / "v3/bootstrap/history_recall.py"
    )
    spec = importlib.util.spec_from_file_location(
        "memory_v2_legacy_composition", composition_path
    )
    composition = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(composition)
    from allday_asr.v3.adapters.sqlite.chat_cache import ChatCache

    ChatCache(folder / "chat/cache.sqlite3")
    service = composition.compose_history_recall(database, folder, source)
    original_run = ModelExecutionRunner.run
    calls = []
    execution_id = str(time.time_ns())

    def run(runner, **kwargs):
        # Preserve legacy prompts, plans and ranking. Replace only execution policy
        # with the common approved native SDK path, avoiding the legacy HTTP proxy.
        phase = "baseline:" + kwargs["task"]
        kwargs["task"] = "memory-core-v2:" + phase
        kwargs["model"] = shared_model.model
        runner.receipt_dir = shared_model.root / "receipts"
        runner.timeout_seconds = 90
        runner.batch_deadline_seconds = 180
        runner.max_batch_calls = 4
        runner.max_attempts = 1
        runner.max_prompt_bytes = 60000
        kwargs["batch_id"] = "memory-v2-baseline:" + case["id"] + ":" + execution_id
        prepared = runner.prepare_request(
            **{
                k: kwargs[k]
                for k in ("task", "prompt", "instructions", "schema", "model", "effort")
            }
        )
        if len(calls) >= 3:
            raise RuntimeError("COMPARISON_QUERY_GENERATION_LIMIT_3")
        from .model import validate_schema_refs

        validate_schema_refs(prepared["schema"])
        n = shared_model.ledger.reserve(
            phase, len(json.dumps(prepared, ensure_ascii=False).encode()) + 32768
        )
        calls.append({"number": n, "request": prepared})
        try:
            result = original_run(runner, **kwargs)
            shared_model.ledger.finish(n, result.usage)
            calls[-1]["raw_response"] = result.final_response
            return result
        except BaseException as exc:
            shared_model.ledger.finish(n, error=exc)
            calls[-1]["error"] = str(exc)
            raise

    request = {
        "action": "query",
        "query_id": "v2-baseline-" + case["id"],
        "question": case["question"],
        "keyword": "",
        "person_id": "",
        "source": "all",
        "timezone": "Asia/Singapore",
        "summarize": True,
        "expand": True,
    }
    started = time.monotonic()
    result = {}
    try:
        with (
            patch.object(ModelExecutionRunner, "run", run),
            patch.object(
                history,
                "time",
                SimpleNamespace(
                    time=lambda: case["reference"], monotonic=time.monotonic
                ),
            ),
        ):
            result = service.execute("isolated-comparison-owner", request)
    except Exception as exc:
        result = {
            "error": type(exc).__name__,
            "detail": str(exc),
            "cause": repr(exc.__cause__),
        }
    finally:
        (folder / "model-calls.json").write_text(
            json.dumps(calls, ensure_ascii=False, default=str, indent=2), "utf-8"
        )
        (folder / "source-queries.json").write_text(
            json.dumps(source.calls, ensure_ascii=False, default=str), "utf-8"
        )
        (folder / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, default=str, indent=2), "utf-8"
        )
        internal = service.results.get(
            ("isolated-comparison-owner", request["query_id"])
        )
        if internal:
            (folder / "internal.json").write_text(
                json.dumps(internal[1], ensure_ascii=False, default=str), "utf-8"
            )
    return {
        "result": result,
        "elapsed_seconds": time.monotonic() - started,
        "model_attempts": len(calls),
        "audit": str(folder),
    }


async def compare(core, snapshot, grant, cases_path, project, root):
    cases = json.loads(Path(cases_path).read_text("utf-8"))
    cases.sort(
        key=lambda c: {"D3": 0, "T1": 1, "M1": 2, "D2": 3, "D1": 4}.get(c["id"], 4)
    )
    report = {
        "kind": "exposed development comparison; not independent generalization",
        "same_snapshot": True,
        "same_answer_model": core.model.model,
        "input_byte_limit": 60000,
        "baseline": "0.8.28 unchanged retrieval; common native SDK execution wrapper",
        "cases": [],
    }
    # Reserve six attempts for shared overview, revisions and transformed checks.
    core.model.ledger.max_calls = 43
    for case in cases:
        completed = root / "comparison" / (case["id"] + ".json")
        completed.parent.mkdir(exist_ok=True)
        if completed.exists():
            report["cases"].append(json.loads(completed.read_text("utf-8")))
            continue
        row = {
            "case": case,
            "source_availability": "full frozen windows; outside windows unknown",
        }
        row["B"] = await asyncio.to_thread(baseline, snapshot, case, root, core.model)
        started = time.monotonic()
        try:
            existing_c = root / "comparison" / (case["id"] + ".C.json")
            if existing_c.exists():
                row["C"] = json.loads(existing_c.read_text("utf-8"))
            else:
                row["C"] = {
                    "result": await core.answer(
                        case["question"], case["reference"], grant
                    ),
                    "elapsed_seconds": time.monotonic() - started,
                }
        except Exception as exc:
            row["C"] = {
                "error": type(exc).__name__,
                "detail": str(exc),
                "elapsed_seconds": time.monotonic() - started,
            }
        # Correctness is deliberately not inferred from nonempty summaries or ID presence.
        row["semantic_assessment"] = "PENDING_EVIDENCE_REVIEW"
        completed.write_text(json.dumps(row, ensure_ascii=False, indent=2), "utf-8")
        report["cases"].append(row)
    core.model.ledger.max_calls = 48
    return report
