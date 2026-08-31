from __future__ import annotations

from pathlib import Path
from typing import Any

from allday_asr.config import load_config
from allday_asr.interfaces.web.presenters import daily_summary_payload
from allday_asr.interfaces.transfer.workflow import build_workflow_processor
from allday_asr.services.daily import run_daily


class BackgroundJobUseCases:
    def start_quality_workflow(
        self,
        session_id: int,
        *,
        shadow: bool,
    ) -> dict:
        database = self.database()
        database.get_recording_session(session_id)
        manifest = database.get_session_manifest(session_id)
        if manifest is None:
            raise ValueError("网页启动 V2 目前只适用于 manifest 分片会话")
        with self.workflow_job_lock:
            for active_session_id, existing_id in self.workflow_jobs.items():
                existing = self.job_registry.get(existing_id)
                if existing["status"] in {"queued", "running"}:
                    if active_session_id == session_id:
                        return existing
                    raise ValueError(
                        f"会话 {active_session_id} 的 V2 正在运行，请等待完成"
                    )
            job = self.job_registry.create(
                kind="quality_workflow_v2",
                session_id=session_id,
                admission_mode="shadow" if shadow else "production",
                detail="等待开始",
            )
            job_id = str(job["id"])
            self.workflow_jobs[session_id] = job_id

        processor = build_workflow_processor(
            config_path=self.config_path,
            database_path=self.database_path,
            admission_mode="shadow" if shadow else "production",
            profile=None,
            diarization_model_path=None,
            backup_root=None,
            backup_storage_kind="independent_device",
        )
        manifest_path = Path(str(manifest["manifest_path"]))

        def worker() -> None:
            self._update_job(
                job_id,
                status="running",
                stage="manifest_import",
                detail="校验会话输入",
            )

            def progress(stage: str, detail: str) -> None:
                self._update_job(job_id, stage=stage, detail=detail)

            try:
                result = dict(processor(manifest_path, progress))
                if int(result["session_id"]) != session_id:
                    raise RuntimeError("V2 工作流返回了不同的 session_id")
                self._update_job(
                    job_id,
                    status="completed",
                    stage="completed",
                    detail="V2 工作流已完成",
                    result=result,
                )
            except Exception as exc:
                self._update_job(
                    job_id,
                    status="failed",
                    stage="failed",
                    detail="V2 工作流失败",
                    error=str(exc),
                )

        self.job_registry.launch(target=worker, name=f"workflow-v2-{job_id[:8]}")
        return dict(job)

    def start_daily_run(self, recording_id: int) -> dict:
        self.database().get_recording(recording_id)
        job = self.job_registry.create(
            recording_id=recording_id,
            detail="等待开始",
        )
        job_id = str(job["id"])

        def worker() -> None:
            self._update_job(job_id, status="running", stage="starting", detail="读取配置")

            def progress(stage: str, detail: str) -> None:
                self._update_job(job_id, stage=stage, detail=detail)

            try:
                summary = run_daily(
                    self.database(),
                    recording_id,
                    load_config(self.config_path),
                    progress=progress,
                )
                result = daily_summary_payload(summary)
                self._update_job(
                    job_id,
                    status="completed",
                    stage="completed",
                    detail="一键离线日记已完成",
                    result=result,
                )
            except Exception as exc:
                self._update_job(
                    job_id,
                    status="failed",
                    stage="failed",
                    detail="运行失败",
                    error=str(exc),
                )

        self.job_registry.launch(target=worker, name=f"daily-run-{job_id[:8]}")
        return dict(job)

    def job(self, job_id: str) -> dict:
        return self.job_registry.get(job_id)

    def _update_job(self, job_id: str, **values: Any) -> None:
        self.job_registry.update(job_id, **values)
