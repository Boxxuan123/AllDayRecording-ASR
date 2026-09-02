from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
WorkflowPostProcessor = Callable[
    [UploadRecord, Mapping[str, Any], ProgressCallback], Mapping[str, Any]
]


class AutomaticWorkflowRunner:
    """Run one durable, serial V2 queue behind the transfer HTTP service."""

    def __init__(
        self,
        inbox: Path,
        *,
        processor: WorkflowProcessor,
        configuration: Mapping[str, Any] | None = None,
        postprocessor: WorkflowPostProcessor | None = None,
        postprocessor_configuration: Mapping[str, Any] | None = None,
    ) -> None:
        self.inbox = inbox.expanduser().resolve()
        self.state_root = self.inbox / ".uploads"
        self.state_root.mkdir(parents=True, exist_ok=True)
        self._processor = processor
        self._postprocessor = postprocessor
        configuration_json = json.dumps(
            dict(configuration or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        self.configuration_sha256 = hashlib.sha256(configuration_json).hexdigest()
        postprocessor_json = json.dumps(
            dict(postprocessor_configuration or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        self.postprocessor_configuration_sha256 = hashlib.sha256(
            postprocessor_json
        ).hexdigest()
        self._queue: queue.Queue[
            tuple[UploadRecord, Path, dict[str, Any] | None] | None
        ] = queue.Queue()
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
            reusable_result: dict[str, Any] | None = None
            if previous is not None and (
                previous.get("configuration_sha256")
                == self.configuration_sha256
            ):
                raw_result = previous.get("result")
                if isinstance(raw_result, dict):
                    reusable_result = dict(raw_result)
                postprocessing_current = (
                    self._postprocessor is None
                    or (
                        previous.get("postprocessor_configuration_sha256")
                        == self.postprocessor_configuration_sha256
                        and reusable_result is not None
                        and "v3" in reusable_result
                    )
                )
                if previous.get("status") == "completed" and postprocessing_current:
                    return previous
            self._active_ids.add(record.upload_id)
            state = self._state(
                record,
                status="queued",
                detail=(
                    "V2 结果已存在，等待 V3 自动回填与生成"
                    if reusable_result is not None
                    else "会话清单已完整接收，等待自动导入和 V2 工作流"
                ),
                result=reusable_result,
            )
            self._write_state(record.upload_id, state)
            self._queue.put((record, manifest_path, reusable_result))
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
            record, manifest_path, reusable_result = item
            try:
                self._run_one(record, manifest_path, reusable_result)
            finally:
                with self._lock:
                    self._active_ids.discard(record.upload_id)
                self._queue.task_done()

    def _run_one(
        self,
        record: UploadRecord,
        manifest_path: Path,
        reusable_result: dict[str, Any] | None,
    ) -> None:
        def progress(stage: str, detail: str) -> None:
            state = self._state(
                record,
                status="running",
                stage=stage,
                detail=detail,
            )
            self._write_state(record.upload_id, state)
            print(f"[transfer:v2] {record.relative_path} | {stage} | {detail}")

        result = dict(reusable_result) if reusable_result is not None else None
        try:
            if result is None:
                progress("manifest_import", "正在校验并导入会话清单")
                result = dict(self._processor(manifest_path, progress))
            else:
                progress("v3_postprocess_resume", "复用已完成的 V2 工作流结果")
            if self._postprocessor is not None:
                progress("v3_postprocess", "正在回填 V3 并生成记忆与待审核内容")
                result["v3"] = dict(
                    self._postprocessor(record, result, progress)
                )
        except Exception as exc:
            state = self._state(
                record,
                status="failed",
                stage="failed",
                detail=str(exc),
                error=repr(exc),
                result=result,
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
            detail=(
                "V2 分析、V3 回填和自动生成已完成"
                if self._postprocessor is not None
                else "V2 工作流已完成"
            ),
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
            "postprocessor_configuration_sha256": (
                self.postprocessor_configuration_sha256
            ),
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


def build_v3_postprocessor(
    *,
    database_path: Path,
    v3_state_dir: Path,
    source_namespace: str | None = None,
    reasoning_effort: str = "auto",
    core_factory: Callable[[], Any] | None = None,
) -> WorkflowPostProcessor:
    """Project one completed V2 session into V3 and run bounded generators."""

    if reasoning_effort not in {"auto", "low", "medium", "high", "xhigh"}:
        raise ValueError(
            "V3 自动生成 reasoning effort 必须是 auto、low、medium、high 或 xhigh"
        )
    selected_namespace = source_namespace.strip() if source_namespace else None
    if source_namespace is not None and not selected_namespace:
        raise ValueError("V3 legacy source namespace 不能为空")
    resolved_database = database_path.expanduser().resolve(strict=True)
    resolved_v3_state = v3_state_dir.expanduser().resolve()

    if core_factory is None:
        from allday_asr.v3.bootstrap import V3CorePaths, compose_v3_core
        from allday_asr.v3.config import CodexReminderSettings

        codex_settings = replace(
            CodexReminderSettings.from_environment(),
            allow_auto_apply=False,
        )

        def default_core_factory():
            return compose_v3_core(
                V3CorePaths.from_state_dir(resolved_v3_state),
                codex_settings=codex_settings,
            )

        selected_core_factory = default_core_factory
    else:
        selected_core_factory = core_factory

    def postprocess(
        record: UploadRecord,
        workflow_result: Mapping[str, Any],
        progress: ProgressCallback,
    ) -> Mapping[str, Any]:
        from allday_asr.v3.application import LegacyImportCommand

        raw_legacy_session_id = workflow_result.get("session_id")
        if isinstance(raw_legacy_session_id, bool):
            raise ValueError("自动 V2 结果缺少有效 session_id")
        try:
            legacy_session_id = int(raw_legacy_session_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("自动 V2 结果缺少有效 session_id") from exc
        if legacy_session_id < 1:
            raise ValueError("自动 V2 结果缺少有效 session_id")

        core = selected_core_factory()
        core.initialize()
        try:
            namespace = selected_namespace or _legacy_namespace(
                core, resolved_database
            )
            progress("v3_legacy_import", "正在把 V2 证据和语义结果幂等回填到 V3")
            from allday_asr.v3.adapters.legacy_v2 import compose_legacy_v2_import

            imported = compose_legacy_v2_import(core).execute(
                LegacyImportCommand(
                    source_database=resolved_database,
                    source_namespace=namespace,
                )
            )
            legacy_ref = (
                f"v2:{namespace}:recording_sessions:{legacy_session_id}"
            )
            with core.database.read() as connection:
                row = connection.execute(
                    "SELECT session_id FROM recording_sessions WHERE legacy_ref = ?",
                    (legacy_ref,),
                ).fetchone()
            if row is None:
                raise RuntimeError(
                    "V2 工作流会话未出现在 V3 回填结果中："
                    f"{legacy_session_id}"
                )
            v3_session_id = str(row["session_id"])
            detail = core.desktop.session_detail(v3_session_id)

            progress("v3_person_memory", "正在刷新跨会话人物记忆")
            memory_results = [
                core.person_memory.refresh(
                    str(person["person_id"]),
                    actor="system:auto-workflow",
                )
                for person in core.people.list_people()
            ]

            settings = core.desktop.settings()
            reminder_result: dict[str, Any] | None = None
            active_utterances = [
                item
                for item in detail["utterances"]
                if item.get("status") == "active"
                and str(item.get("text", "")).strip()
            ]
            if settings["reminders"]["codex_enabled"] and active_utterances:
                progress("v3_reminder_generation", "正在生成待人工确认的提醒候选")
                reminder_result = core.reminder_extraction.extract(
                    v3_session_id,
                    reasoning_effort=reasoning_effort,
                )

            daily_result: dict[str, Any] | None = None
            session = detail["session"]
            timezone_name = _canonical_timezone_name(str(session["timezone"]))
            summary_date = _local_date(
                str(session["captured_start"]), timezone_name
            )
            if settings["insights"]["codex_enabled"] and active_utterances:
                progress("v3_daily_insight", "正在生成有证据引用的当日洞察")
                daily_result = core.insights.generate_daily(
                    summary_date,
                    timezone_name,
                    reasoning_effort=reasoning_effort,
                )

            candidates = (
                reminder_result.get("candidates", [])
                if reminder_result is not None
                else []
            )
            pending_candidates = [
                item
                for item in candidates
                if item.get("status") == "pending_confirmation"
            ]
            processing_reviews = core.desktop.list_reviews(limit=500)
            return {
                "status": "completed",
                "upload_id": record.upload_id,
                "legacy_session_id": legacy_session_id,
                "session_id": v3_session_id,
                "source_namespace": namespace,
                "legacy_import": {
                    "import_id": imported.import_id,
                    "created": imported.created,
                    "existing": imported.existing,
                    "issue_count": len(imported.issues),
                },
                "person_memories": memory_results,
                "reminder_generation": reminder_result,
                "daily_insight": daily_result,
                "pending_reminder_review_count": len(pending_candidates),
                "processing_review_count": len(processing_reviews),
                "human_review_required": bool(
                    pending_candidates or processing_reviews
                ),
                "reasoning_effort": reasoning_effort,
                "automatic_approval": False,
            }
        finally:
            core.close()

    return postprocess


def _legacy_namespace(core: Any, source_database: Path) -> str:
    with core.database.read() as connection:
        row = connection.execute(
            """
            SELECT source_namespace
            FROM legacy_import_runs
            WHERE lower(source_path) = lower(?)
            ORDER BY started_at DESC, import_id DESC
            LIMIT 1
            """,
            (str(source_database),),
        ).fetchone()
    if row is not None:
        return str(row["source_namespace"])
    normalized = str(source_database).replace("\\", "/").casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def _local_date(captured_start: str, timezone_name: str) -> str:
    captured = datetime.fromisoformat(captured_start.replace("Z", "+00:00"))
    if captured.tzinfo is None:
        raise ValueError("V3 session captured_start must include a timezone")
    return captured.astimezone(ZoneInfo(timezone_name)).date().isoformat()


def _canonical_timezone_name(timezone_name: str) -> str:
    """Convert timezone labels emitted by legacy phone builds to IANA names."""

    candidate = timezone_name.strip()
    aliases = {
        # Legacy HarmonyOS builds emitted the ambiguous UTC+8 label CST. Keep
        # imported sessions on this project's configured business-day boundary.
        "CST": "Asia/Singapore",
    }
    canonical = aliases.get(candidate.upper(), candidate)
    try:
        ZoneInfo(canonical)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"V3 session timezone is not an IANA timezone: {candidate}") from exc
    return canonical


__all__ = [
    "AUTOMATION_STATE_VERSION",
    "AutomaticWorkflowRunner",
    "WorkflowPostProcessor",
    "WorkflowProcessor",
    "build_v3_postprocessor",
    "build_workflow_processor",
]
