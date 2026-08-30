from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from typing import Any


class JobRegistry:
    """Thread-safe in-memory status registry for local background jobs."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create(self, **values: Any) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "status": "queued",
            "stage": "queued",
            "result": None,
            "error": None,
            **values,
        }
        with self._lock:
            self._jobs[job_id] = job
        return dict(job)

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(f"任务 {job_id} 不存在")
            return dict(job)

    def update(self, job_id: str, **values: Any) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(f"任务 {job_id} 不存在")
            job.update(values)
            return dict(job)

    @staticmethod
    def launch(*, target: Callable[[], None], name: str) -> threading.Thread:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        return thread
