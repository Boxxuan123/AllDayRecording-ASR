from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from allday_asr.application.semantic.pipeline import (
    SemanticSettings as SemanticV2E02Settings,
)
from allday_asr.config import load_config
from allday_asr.interfaces.cli.runtime import (
    build_quality_asr_runtime,
    build_quality_diarization_runtime,
)
from allday_asr.interfaces.transfer.store import UploadRecord
from allday_asr.services.quality_workflow import run_quality_workflow
from allday_asr.services.session_backup import (
    PRODUCTION_STORAGE_KINDS,
    create_session_backup,
)
from allday_asr.services.session_ingest import ingest_session_manifest
from allday_asr.storage.database import Database


AUTOMATION_STATE_VERSION = 1
_UPLOAD_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
ProgressCallback = Callable[[str, str], None]
WorkflowProcessor = Callable[[Path, ProgressCallback], Mapping[str, Any]]


class AutomaticWorkflowRunner:
    """Run one durable, serial V2 queue behind the transfer HTTP service."""

    def __init__(
        self,
        inbox: Path,
        *,
        processor: WorkflowProcessor,
        configuration: Mapping[str, Any] | None = None,
    ) -> None:
        self.inbox = inbox.expanduser().resolve()
        self.state_root = self.inbox / ".uploads"
        self.state_root.mkdir(parents=True, exist_ok=True)
        self._processor = processor
        configuration_json = json.dumps(
            dict(configuration or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        self.configuration_sha256 = hashlib.sha256(configuration_json).hexdigest()
        self._queue: queue.Queue[tuple[UploadRecord, Path] | None] = queue.Queue()
        self._lock = threading.RLock()
        self._active_ids: set[str] = set()
        self._closed = False
        self._worker = threading.Thread(
            target=self._work,
            name="allday-v2-auto-workflow",
            daemon=False,
        )
        self._worker.start()

    def submit(self, record: UploadRecord) -> dict[str, Any] | None:
        if record.kind != "manifest" or record.status != "completed":
            return None
        manifest_path = self._completed_path(record)
        with self._lock:
            if self._closed:
                raise RuntimeError("自动 V2 工作流队列已经关闭")
            if record.upload_id in self._active_ids:
                return self.get_status(record.upload_id)
            previous = self.get_status(record.upload_id)
            if (
                previous is not None
                and previous.get("status") == "completed"
                and previous.get("configuration_sha256")
                == self.configuration_sha256
            ):
                return previous
            self._active_ids.add(record.upload_id)
            state = self._state(
                record,
                status="queued",
                detail="会话清单已完整接收，等待自动导入和 V2 工作流",
            )
            self._write_state(record.upload_id, state)
            self._queue.put((record, manifest_path))
            return state

    def get_status(self, upload_id: str) -> dict[str, Any] | None:
        path = self._state_path(upload_id)
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"自动工作流状态损坏：{upload_id}") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"自动工作流状态不是 JSON object：{upload_id}")
        return value

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._queue.put(None)
        self._worker.join()

    def _work(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            record, manifest_path = item
            try:
                self._run_one(record, manifest_path)
            finally:
                with self._lock:
                    self._active_ids.discard(record.upload_id)
                self._queue.task_done()

    def _run_one(self, record: UploadRecord, manifest_path: Path) -> None:
        def progress(stage: str, detail: str) -> None:
            state = self._state(
                record,
                status="running",
                stage=stage,
                detail=detail,
            )
            self._write_state(record.upload_id, state)
            print(f"[transfer:v2] {record.relative_path} | {stage} | {detail}")

        try:
            progress("manifest_import", "正在校验并导入会话清单")
            result = dict(self._processor(manifest_path, progress))
        except Exception as exc:
            state = self._state(
                record,
                status="failed",
                stage="failed",
                detail=str(exc),
                error=repr(exc),
            )
            self._write_state(record.upload_id, state)
            print(
                f"[transfer:v2] 自动工作流失败 {record.relative_path}：{exc!r}"
            )
            return
        state = self._state(
            record,
            status="completed",
            stage="completed",
            detail="V2 工作流已完成",
            result=result,
        )
        self._write_state(record.upload_id, state)
        print(
            f"[transfer:v2] 自动工作流完成 {record.relative_path} | "
            f"session={result.get('session_id')} | "
            f"workflow_run={result.get('workflow_run_id')} | "
            f"state={result.get('workflow_state')}"
        )

    def _completed_path(self, record: UploadRecord) -> Path:
        path = (self.inbox / Path(*PurePosixPath(record.relative_path).parts)).resolve()
        try:
            path.relative_to(self.inbox)
        except ValueError as exc:
            raise RuntimeError("自动工作流清单越过接收目录") from exc
        if not path.is_file() or path.stat().st_size != record.size:
            raise RuntimeError(f"已完成的会话清单缺失：{record.relative_path}")
        return path

    def _state_path(self, upload_id: str) -> Path:
        if not _UPLOAD_ID_PATTERN.fullmatch(upload_id):
            raise ValueError("自动工作流 upload_id 无效")
        return self.state_root / f"{upload_id}.workflow.json"

    def _write_state(self, upload_id: str, value: Mapping[str, Any]) -> None:
        path = self._state_path(upload_id)
        temporary = self.state_root / f".{upload_id}.workflow.json.tmp"
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def _state(
        self,
        record: UploadRecord,
        *,
        status: str,
        detail: str,
        stage: str | None = None,
        error: str | None = None,
        result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        value: dict[str, Any] = {
            "version": AUTOMATION_STATE_VERSION,
            "upload_id": record.upload_id,
            "relative_path": record.relative_path,
            "manifest_sha256": record.sha256,
            "configuration_sha256": self.configuration_sha256,
            "status": status,
            "stage": stage,
            "detail": detail,
            "updated_at": datetime.now(timezone.utc).isoformat(
                timespec="milliseconds"
            ),
        }
        if error is not None:
            value["error"] = error
        if result is not None:
            value["result"] = dict(result)
        return value


def build_workflow_processor(
    *,
    config_path: Path,
    database_path: Path,
    admission_mode: str,
    profile: str | None,
    diarization_model_path: Path | None,
    backup_root: Path | None,
    backup_storage_kind: str,
) -> WorkflowProcessor:
    if admission_mode not in {"production", "shadow"}:
        raise ValueError("自动工作流模式必须是 production 或 shadow")
    if admission_mode == "production" and backup_root is None:
        raise ValueError(
            "production 自动工作流必须配置独立 --workflow-backup-root；"
            "若明确接受风险，请使用 --workflow-shadow"
        )
    if (
        backup_root is not None
        and backup_storage_kind not in PRODUCTION_STORAGE_KINDS
    ):
        raise ValueError(
            "自动工作流备份类型必须是 independent_device 或 network"
        )

    resolved_config_path = config_path.expanduser().resolve(strict=True)
    resolved_database_path = database_path.expanduser().resolve()
    resolved_backup_root = (
        backup_root.expanduser().resolve() if backup_root is not None else None
    )
    resolved_model_path = (
        diarization_model_path.expanduser().resolve(strict=True)
        if diarization_model_path is not None
        else None
    )

    def process(
        manifest_path: Path,
        progress: ProgressCallback,
    ) -> Mapping[str, Any]:
        resolved = load_config(resolved_config_path)
        database = Database.open(resolved_database_path)
        ingest = ingest_session_manifest(
            database,
            manifest_path,
            device=resolved.ingest.device,
            timezone_name=resolved.ingest.timezone,
            ingest_method="watch_auto",
        )
        progress(
            "manifest_imported",
            f"session_id={ingest.session_id}，chunks={ingest.chunk_count}",
        )

        backup_id: int | None = None
        if resolved_backup_root is not None:
            backup = create_session_backup(
                database,
                ingest.session_id,
                resolved_backup_root,
                storage_kind=backup_storage_kind,
                restore_drill=True,
            )
            backup_id = backup.backup_id
            progress(
                "backup_verified",
                f"backup_id={backup.backup_id}，files={backup.file_count}",
            )

        asr_runtime = build_quality_asr_runtime(
            resolved,
            requested_profile=profile,
        )
        diarization_runtime = build_quality_diarization_runtime(
            resolved,
            model_path=resolved_model_path,
        )
        summary = run_quality_workflow(
            database,
            None,
            session_id=ingest.session_id,
            asr_settings=asr_runtime.settings,
            diarization_settings=diarization_runtime.settings,
            semantic_settings=SemanticV2E02Settings(),
            primary_factory=asr_runtime.primary_factory,
            secondary_factory=asr_runtime.secondary_factory,
            diarization_factory=diarization_runtime.backend_factory,
            admission_mode=admission_mode,
            progress=progress,
        )
        return {
            "session_id": ingest.session_id,
            "manifest_created": ingest.created,
            "backup_id": backup_id,
            "workflow_run_id": summary.workflow_run_id,
            "workflow_state": summary.state,
            "asr_run_id": summary.asr_run_id,
            "diarization_run_id": summary.diarization_run_id,
            "semantic_run_id": summary.semantic_run_id,
            "review_required": summary.review_required,
        }

    return process


__all__ = [
    "AUTOMATION_STATE_VERSION",
    "AutomaticWorkflowRunner",
    "WorkflowProcessor",
    "build_workflow_processor",
]
