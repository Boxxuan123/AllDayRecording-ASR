"""A finite explicit pass reusing model execution receipts and publication idempotency."""
import json
import re
import threading
import time
from datetime import date
from pathlib import Path

from .daily_backfill import atomic_json
from .file_lock import day_worker_lock
from .generation_context import GenerationContext, generation_context


class ScopedRecomputePass:
    MAX_ITEMS = 8
    MAX_ATTEMPTS = 2

    def __init__(self, core, journal_dir: Path, *, execution_id, stage, targets,
                 timezone_name="Asia/Singapore", timeout_seconds=180):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", execution_id):
            raise ValueError("execution_id must be 1-80 safe characters")
        if stage not in {"events", "summary"}:
            raise ValueError("stage must be events or summary")
        if not 1 <= len(targets) <= self.MAX_ITEMS or len(set(targets)) != len(targets):
            raise ValueError("provide 1-8 distinct explicit targets")
        if not 1 <= timeout_seconds <= 900:
            raise ValueError("pass timeout must be 1-900 seconds")
        if stage == "summary":
            for target in targets:
                date.fromisoformat(target)
        self.core, self.stage, self.targets = core, stage, list(targets)
        self.execution_id, self.timezone_name = execution_id, timezone_name
        self.path = Path(journal_dir) / f"scope-{execution_id}.pass.json"
        self.cancel_path = Path(str(self.path).removesuffix(".json") + ".cancel")
        self.scope = {"execution_id": execution_id, "stage": stage, "targets": list(targets),
                      "timezone": timezone_name, "timeout_seconds": timeout_seconds}
        self.timeout_seconds = timeout_seconds

    def cancel(self):
        self.cancel_path.parent.mkdir(parents=True, exist_ok=True)
        self.cancel_path.write_text("cancelled", encoding="utf-8")

    def run(self):
        with day_worker_lock(self.path) as acquired:
            if not acquired:
                raise RuntimeError("scoped pass is already running")
            return self._run()

    def _run(self):
        if self.path.exists():
            journal = json.loads(self.path.read_text("utf-8"))
            if journal["scope"] != self.scope:
                raise ValueError("execution ID already belongs to another scope")
        else:
            journal = {"scope": self.scope, "status": "queued",
                       "deadline": time.time() + self.timeout_seconds,
                       "items": [{"target": t, "status": "queued", "attempts": 0} for t in self.targets]}
            atomic_json(self.path, journal)
        if journal["status"] in {"succeeded", "cancelled", "timeout"}:
            return journal
        context = GenerationContext(self.execution_id, journal["deadline"])
        stopped = threading.Event()

        def watch_cancel():
            while not stopped.wait(.1):
                if self.cancel_path.exists():
                    context.cancelled.set()
                    return

        watcher = threading.Thread(target=watch_cancel, daemon=True, name="scoped-generation-cancel")
        watcher.start()
        journal["status"] = "running"
        atomic_json(self.path, journal)
        try:
            for item in journal["items"]:
                if item["status"] == "succeeded" or item["attempts"] >= self.MAX_ATTEMPTS:
                    continue
                if self.cancel_path.exists():
                    context.cancelled.set()
                context.check()
                item.update(status="running", attempts=item["attempts"] + 1)
                atomic_json(self.path, journal)
                try:
                    context.receipts.clear()
                    with generation_context(context):
                        if self.stage == "events":
                            result = self.core.semantic_events.extract(item["target"], recompute=True)
                        else:
                            summarize = getattr(self.core.insights, "recompute_summary", None)
                            if summarize is None:
                                summarize = self.core.insights.generate_daily
                            result = summarize(item["target"], self.timezone_name)
                        context.check()
                    item.update(status="succeeded", result=result)
                    item.pop("error_type", None)
                except (InterruptedError, TimeoutError):
                    raise
                except Exception as exc:
                    context.check()
                    item.update(status="failed", error_type=type(exc).__name__)
                atomic_json(self.path, journal)
            journal["status"] = "succeeded" if all(i["status"] == "succeeded" for i in journal["items"]) else "failed"
        except TimeoutError:
            journal["status"] = "timeout"
        except (KeyboardInterrupt, InterruptedError):
            context.cancelled.set()
            journal["status"] = "cancelled"
        finally:
            stopped.set()
            watcher.join(timeout=1)
            for item in journal["items"]:
                if item["status"] in {"running", "queued"}:
                    item["status"] = journal["status"] if journal["status"] in {"cancelled", "timeout"} else "not_run"
            journal["completed_at"] = time.time()
            atomic_json(self.path, journal)
        return journal
