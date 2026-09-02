from __future__ import annotations

import os
import queue
import socket
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from allday_asr.v3.adapters.backup import FilesystemSessionBackupAdapter
from allday_asr.v3.adapters.transfer.automation_state import (
    AutomaticWorkflowStateStore,
)
from allday_asr.v3.adapters.transfer.automation_retry import (
    AutomaticWorkflowRetryMixin,
)
from allday_asr.v3.application import (
    DurableProcessingWorker,
    RecordBackupEvidenceCommand,
    SubmitProcessingCommand,
)


class V3AutomaticWorkflowRunner(AutomaticWorkflowRetryMixin):
    """Durable V3-only processing triggered after a Phone manifest is ingested."""

    def __init__(
        self,
        core: Any,
        model_adapter: Any,
        *,
        backup_root: Path | None,
        backup_storage_kind: str = "independent_device",
        shadow: bool = False,
        reasoning_effort: str = "auto",
        pipeline_version: str = "v3-native.1",
        worker_id: str | None = None,
        max_auto_retries: int = 3,
        retry_delays: tuple[float, ...] = (5.0, 30.0, 120.0),
        poll_interval: float = 1.0,
    ) -> None:
        if backup_storage_kind not in {"independent_device", "network"}:
            raise ValueError("V3 backup storage kind is invalid")
        if reasoning_effort not in {"auto", "low", "medium", "high", "xhigh"}:
            raise ValueError("V3 reasoning effort is invalid")
        if max_auto_retries < 0:
            raise ValueError("automatic workflow retry count cannot be negative")
        if len(retry_delays) < max_auto_retries or any(
            delay < 0 for delay in retry_delays
        ):
            raise ValueError("automatic workflow retry delays are invalid")
        if poll_interval <= 0:
            raise ValueError("automatic workflow poll interval must be positive")
        self.core = core
        self.model_adapter = model_adapter
        if not shadow and backup_root is None:
            raise ValueError("V3 production automation requires an independent backup root")
        self.backup_root = (
            backup_root.expanduser().resolve() if backup_root is not None else None
        )
        self.backup_storage_kind = backup_storage_kind
        self.shadow = shadow
        self.reasoning_effort = reasoning_effort
        self.pipeline_version = pipeline_version
        self.max_auto_retries = max_auto_retries
        self.retry_delays = retry_delays
        self.poll_interval = poll_interval
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:receiver"
        self.state_root = core.paths.state_dir / "automation"
        self._states = AutomaticWorkflowStateStore(self.state_root)
        self._states.initialize()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._active: set[str] = set()
        self._lock = threading.RLock()
        self._closed = False
        self._worker = DurableProcessingWorker(
            core.processing,
            model_adapter,
            worker_id=self.worker_id,
            config={"pipeline": pipeline_version},
        )
        self._thread = threading.Thread(
            target=self._run,
            name="allday-v3-native-workflow",
            daemon=True,
        )
        self._recover_pending()
        self._thread.start()

    def submit(
        self, record: Any, ingest_result: Mapping[str, Any]
    ) -> dict[str, Any]:
        session_id = str(ingest_result.get("session_id") or "")
        if not session_id:
            raise ValueError("V3 ingest result has no session identity")
        with self._lock:
            previous = self.status(session_id)
            if previous is not None and previous.get("status") == "completed":
                return previous
            if session_id not in self._active:
                self._active.add(session_id)
                now = _utc_now()
                queued = {
                    "version": 1,
                    "status": "queued",
                    "stage": "backup",
                    "detail": "V3 会话已入库，等待独立备份和原生分析",
                    "session_id": session_id,
                    "upload_id": str(record.upload_id),
                    "created_at": (
                        str(previous.get("created_at"))
                        if previous is not None and previous.get("created_at")
                        else now
                    ),
                    "updated_at": now,
                    "attempt_count": int((previous or {}).get("attempt_count") or 0),
                    "auto_retry_count": 0,
                    "max_auto_retries": getattr(self, "max_auto_retries", 3),
                }
                # A retry may follow successful native processing but failed
                # post-processing. Keep the completed job identity so _process
                # can reuse it instead of transcribing the same session again.
                if previous is not None and previous.get("job_id"):
                    queued["job_id"] = str(previous["job_id"])
                self._write(
                    session_id,
                    queued,
                )
                self._queue.put(session_id)
        return self.status(session_id) or {"status": "queued", "session_id": session_id}

    def status(self, session_id: str) -> dict[str, Any] | None:
        return self._states.status(session_id)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._queue.put(None)
        self._thread.join()

    def _run(self) -> None:
        while True:
            self._poll_persistent_work()
            try:
                session_id = self._queue.get(timeout=self.poll_interval)
            except queue.Empty:
                continue
            if session_id is None:
                self._queue.task_done()
                return
            current = self.status(session_id) or {}
            attempt_count = int(current.get("attempt_count") or 0) + 1
            self._progress(
                session_id,
                "running",
                str(current.get("stage") or "backup"),
                "自动流水线正在执行",
                attempt_count=attempt_count,
                last_started_at=_utc_now(),
                next_retry_at=None,
                error=None,
            )
            try:
                self._process(session_id)
            except Exception as exc:  # noqa: BLE001 - persist the complete failure
                self._handle_failure(session_id, exc)
            finally:
                with self._lock:
                    self._active.discard(session_id)
                self._queue.task_done()

    def _process(self, session_id: str) -> None:
        backup = None
        if self.shadow:
            self._progress(
                session_id,
                "running",
                "backup_shadow",
                "显式 shadow：保留备份阻断状态，仅运行可审计的 V3 开发分析",
            )
        elif not self._session_is_admitted(session_id):
            self._progress(session_id, "running", "backup", "正在创建并回读校验 V3 独立备份")
            assert self.backup_root is not None
            backup = FilesystemSessionBackupAdapter(
                self.core.database,
                self.core.audio_store,
                self.core.artifact_store,
            ).backup(
                session_id,
                self.backup_root,
                storage_kind=self.backup_storage_kind,
            )
            self.core.admission.record_verified_backup(
                RecordBackupEvidenceCommand(
                    session_id=session_id,
                    provider=backup.provider,
                    storage_kind=backup.storage_kind,
                    digest=backup.digest,
                    restore_checked_at=datetime.now(timezone.utc),
                    metadata={
                        "destination": str(backup.destination),
                        "file_count": backup.file_count,
                        "byte_count": backup.byte_count,
                        "restore_drill": True,
                    },
                )
            )
        else:
            self._progress(
                session_id,
                "running",
                "backup_verified",
                "独立备份证据已存在，继续执行后续完整流程",
            )

        snapshot, reused = self._processing_snapshot(session_id)
        if reused:
            detail = f"继续 V3 原生分析任务：{snapshot.job.job_id}"
        else:
            detail = f"V3 原生分析任务已提交：{snapshot.job.job_id}"
        self._progress(
            session_id,
            "running",
            "processing_reused" if reused else "processing",
            detail,
            job_id=snapshot.job.job_id,
        )
        terminal = {
            "succeeded",
            "failed",
            "failed_final",
            "failed_retryable",
            "cancelled",
        }
        while snapshot.job.status not in terminal:
            worked = self._worker.run_once()
            snapshot = self.core.processing.get(snapshot.job.job_id)
            self._progress(
                session_id,
                "running",
                snapshot.run.current_stage or "processing",
                f"V3 分析进度 {round(snapshot.run.progress * 100)}%",
                job_id=snapshot.job.job_id,
            )
            if not worked and snapshot.job.status not in terminal:
                raise RuntimeError("V3 processing job could not be claimed")
        if snapshot.job.status != "succeeded":
            raise RuntimeError(snapshot.job.error or f"V3 processing ended as {snapshot.job.status}")

        self._progress(session_id, "running", "speaker_identity", "正在分析开放集说话人身份")
        identity = self.core.people.analyze(session_id)
        detail = self.core.desktop.session_detail(session_id)
        active_utterances = [
            value
            for value in detail["utterances"]
            if value.get("status") == "active" and str(value.get("text", "")).strip()
        ]
        reminder_result: dict[str, Any] | None = None
        semantic_event_result: dict[str, Any] | None = None
        daily_result: dict[str, Any] | None = None
        settings = self.core.desktop.settings()
        if (
            active_utterances
            and settings["knowledge"]["codex_event_generation_enabled"]
        ):
            self._progress(
                session_id,
                "running",
                "semantic_event_generation",
                "正在生成并落库带证据的原生语义事件",
            )
            semantic_event_result = self.core.semantic_events.extract(
                session_id,
                reasoning_effort=self.reasoning_effort,
            )
        if active_utterances and settings["reminders"]["codex_enabled"]:
            self._progress(session_id, "running", "reminder_generation", "正在生成待人工审核的提醒")
            reminder_result = self.core.reminder_extraction.extract(
                session_id,
                reasoning_effort=self.reasoning_effort,
            )
        if active_utterances and settings["insights"]["codex_enabled"]:
            self._progress(session_id, "running", "daily_insight", "正在生成带证据引用的当日洞察")
            session = detail["session"]
            timezone_name = _timezone_name(str(session["timezone"]))
            captured = datetime.fromisoformat(str(session["captured_start"]).replace("Z", "+00:00"))
            summary_date = captured.astimezone(ZoneInfo(timezone_name)).date().isoformat()
            daily_result = self.core.insights.generate_daily(
                summary_date,
                timezone_name,
                reasoning_effort=self.reasoning_effort,
            )
        memory_results = [
            self.core.person_memory.refresh(
                str(person["person_id"]), actor="system:v3-native-workflow"
            )
            for person in self.core.people.list_people()
        ]
        reminders = reminder_result.get("candidates", []) if reminder_result else []
        reviews = self.core.desktop.list_reviews(limit=500)
        self._progress(
            session_id,
            "completed",
            "completed",
            "V3 原生分析、Codex 生成和人工审核队列已完成",
            job_id=snapshot.job.job_id,
            result={
                "pipeline": self.pipeline_version,
                "backup_digest": backup.digest if backup is not None else None,
                "admission_mode": "shadow" if self.shadow else "production",
                "speaker_identity": identity,
                "semantic_event_generation": semantic_event_result,
                "reminder_generation": reminder_result,
                "daily_insight": daily_result,
                "person_memories": memory_results,
                "pending_reminder_review_count": sum(
                    value.get("status") == "pending_confirmation" for value in reminders
                ),
                "processing_review_count": len(reviews),
                "auto_accepted_semantic_event_count": (
                    semantic_event_result.get("auto_accepted_count", 0)
                    if semantic_event_result
                    else 0
                ),
                "automatic_approval": False,
            },
            auto_retry_count=0,
            next_retry_at=None,
            needs_manual_retry=False,
            error=None,
        )

    def _session_is_admitted(self, session_id: str) -> bool:
        with self.core.database.read() as connection:
            row = connection.execute(
                "SELECT status_code FROM recording_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"recording session does not exist: {session_id}")
        if str(row["status_code"]) == "ready":
            return True
        return self.core.admission.evaluate(session_id)

    def _processing_snapshot(self, session_id: str) -> tuple[Any, bool]:
        current = self.status(session_id) or {}
        job_id = str(current.get("job_id") or "")
        snapshot = None
        if job_id:
            try:
                candidate = self.core.processing.get(job_id)
            except (KeyError, ValueError):
                candidate = None
            if (
                candidate is not None
                and candidate.run.session_id == session_id
                and candidate.run.pipeline_version == self.pipeline_version
            ):
                snapshot = candidate
        if snapshot is not None:
            if snapshot.job.status in {"failed_retryable", "stale"}:
                return self.core.processing.retry(snapshot.job.job_id), True
            if snapshot.job.status in {"queued", "running", "succeeded"}:
                return snapshot, True
        return (
            self.core.processing.submit(
                SubmitProcessingCommand(
                    session_id=session_id,
                    pipeline_version=self.pipeline_version,
                    input_revision=1,
                    config={"pipeline": self.pipeline_version},
                    admission_mode="shadow" if self.shadow else "production",
                    force_reprocess=snapshot is not None,
                )
            ),
            False,
        )

    def _progress(
        self,
        session_id: str,
        status: str,
        stage: str,
        detail: str,
        **values: Any,
    ) -> None:
        current = self.status(session_id) or {"version": 1, "session_id": session_id}
        current.update(
            {
                "status": status,
                "stage": stage,
                "detail": detail,
                "updated_at": _utc_now(),
                **values,
            }
        )
        self._write(session_id, current)
        print(f"[transfer:v3] {session_id} | {stage} | {detail}")

    def _completed_processing_snapshot(self, session_id: str) -> Any | None:
        current = self.status(session_id) or {}
        job_id = str(current.get("job_id") or "")
        if not job_id:
            return None
        try:
            snapshot = self.core.processing.get(job_id)
        except (KeyError, ValueError):
            return None
        if (
            snapshot.job.status != "succeeded"
            or snapshot.run.session_id != session_id
            or snapshot.run.pipeline_version != self.pipeline_version
        ):
            return None
        return snapshot

    def _write(self, session_id: str, value: Mapping[str, Any]) -> None:
        self._states.save(session_id, value)


def _timezone_name(value: str) -> str:
    aliases = {"CST": "Asia/Singapore"}
    selected = aliases.get(value.strip().upper(), value.strip())
    try:
        ZoneInfo(selected)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"V3 session timezone is invalid: {value}") from exc
    return selected


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = ["V3AutomaticWorkflowRunner"]
