from __future__ import annotations

from dataclasses import asdict
from typing import Any

from allday_asr.config import load_config
from allday_asr.services.daily import run_daily


class BackgroundJobUseCases:
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
                result = {
                    "run_id": summary.run_id,
                    "recording_id": summary.recording_id,
                    "status": summary.status,
                    "steps": [asdict(step) for step in summary.steps],
                    "review_actions": summary.review_actions,
                    "manifest_markdown_path": str(
                        summary.manifest_markdown_path.resolve()
                    ),
                }
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
