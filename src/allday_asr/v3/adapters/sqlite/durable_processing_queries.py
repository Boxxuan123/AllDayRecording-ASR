from __future__ import annotations
from allday_asr.v3.domain.models import ProcessingRun
from allday_asr.v3.domain.processing import (
    ProcessingJob,
    ProcessingSnapshot,
    StageRun,
)
from .processing_repository_codec import (
    _job,
    _run,
    _stage,
    _attempt,
    _datetime,
    _optional_datetime,
    _json,
    _optional_json,
)

_EFFECTIVE = (
    "created",
    "queued",
    "running",
    "waiting_review",
    "failed_retryable",
    "cancel_requested",
    "stale",
)


class DurableProcessingQueryMixin:
    def find_effective_run(
        self, session_id: str, input_revision: int, pipeline_version: str
    ) -> ProcessingRun | None:
        placeholders = ",".join("?" for _ in _EFFECTIVE)
        row = self.connection.execute(
            f"""
            SELECT * FROM processing_runs
            WHERE session_id = ? AND input_revision = ? AND pipeline_version = ?
              AND status IN ({placeholders})
            ORDER BY created_at DESC, run_id DESC LIMIT 1
            """,
            (session_id, input_revision, pipeline_version, *_EFFECTIVE),
        ).fetchone()
        return _run(row) if row is not None else None

    def find_succeeded_run(
        self,
        session_id: str,
        input_revision: int,
        pipeline_version: str,
        config_digest: str,
    ) -> ProcessingRun | None:
        row = self.connection.execute(
            """
            SELECT * FROM processing_runs
            WHERE session_id = ? AND input_revision = ? AND pipeline_version = ?
              AND config_digest = ? AND status = 'succeeded'
            ORDER BY created_at DESC, run_id DESC LIMIT 1
            """,
            (session_id, input_revision, pipeline_version, config_digest),
        ).fetchone()
        return _run(row) if row is not None else None

    def add_graph(
        self,
        run: ProcessingRun,
        job: ProcessingJob,
        stages: tuple[StageRun, ...],
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO processing_runs (
                run_id, session_id, pipeline_version, input_revision, status,
                config_digest, current_stage, progress, completed_at, error,
                legacy_ref, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.run_id,
                run.session_id,
                run.pipeline_version,
                run.input_revision,
                run.status.value,
                run.config_digest,
                run.current_stage,
                run.progress,
                _optional_datetime(run.completed_at),
                run.error,
                run.legacy_ref,
                _datetime(run.created_at),
                _datetime(run.updated_at),
            ),
        )
        self.connection.execute(
            """
            INSERT INTO processing_jobs (
                job_id, run_id, kind, status, priority, request_json,
                result_json, error, available_at, lease_owner, heartbeat_at,
                created_at, updated_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.job_id,
                job.run_id,
                job.kind,
                job.status,
                job.priority,
                _json(job.request),
                _optional_json(job.result),
                job.error,
                _datetime(job.available_at),
                job.lease_owner,
                _optional_datetime(job.heartbeat_at),
                _datetime(job.created_at),
                _datetime(job.updated_at),
                _optional_datetime(job.completed_at),
            ),
        )
        for stage in stages:
            self.connection.execute(
                """
                INSERT INTO stage_runs (
                    stage_run_id, run_id, stage, ordinal, optional, status,
                    progress, output_json, error, created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stage.stage_run_id,
                    stage.run_id,
                    stage.stage,
                    stage.ordinal,
                    int(stage.optional),
                    stage.status.value,
                    stage.progress,
                    _optional_json(stage.output),
                    stage.error,
                    _datetime(stage.created_at),
                    _datetime(stage.updated_at),
                    _optional_datetime(stage.completed_at),
                ),
            )

    def get_snapshot(self, job_id: str) -> ProcessingSnapshot:
        job_row = self.connection.execute(
            "SELECT * FROM processing_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if job_row is None:
            raise KeyError(f"processing job does not exist: {job_id}")
        run_row = self.connection.execute(
            "SELECT * FROM processing_runs WHERE run_id = ?", (job_row["run_id"],)
        ).fetchone()
        stage_rows = self.connection.execute(
            "SELECT * FROM stage_runs WHERE run_id = ? ORDER BY ordinal",
            (job_row["run_id"],),
        ).fetchall()
        attempt_rows = self.connection.execute(
            """
            SELECT a.* FROM stage_attempts a
            JOIN stage_runs s ON s.stage_run_id = a.stage_run_id
            WHERE s.run_id = ? ORDER BY s.ordinal, a.attempt_number
            """,
            (job_row["run_id"],),
        ).fetchall()
        if run_row is None:
            raise RuntimeError("processing job has no run")
        return ProcessingSnapshot(
            job=_job(job_row),
            run=_run(run_row),
            stages=tuple(_stage(row) for row in stage_rows),
            attempts=tuple(_attempt(row) for row in attempt_rows),
        )

    def get_snapshot_for_run(self, run_id: str) -> ProcessingSnapshot:
        row = self.connection.execute(
            "SELECT job_id FROM processing_jobs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"processing run has no durable job: {run_id}")
        return self.get_snapshot(str(row["job_id"]))
