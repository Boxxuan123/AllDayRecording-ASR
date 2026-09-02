from __future__ import annotations
from typing import Any
from allday_asr.v3.domain.processing import (
    ProcessingClaim,
    ProcessingSnapshot,
    StageStatus,
)
from .processing_repository_codec import _job, _run, _stage, _attempt, _lease, _json


class DurableProcessingLifecycleMixin:
    def request_cancel(self, job_id: str, reason: str) -> ProcessingSnapshot:
        snapshot = self.get_snapshot(job_id)
        if snapshot.job.status in {"succeeded", "failed_final", "cancelled"}:
            return snapshot
        now = self.now()
        text = reason[:4000]
        if snapshot.job.status == "running":
            self.connection.execute(
                "UPDATE processing_jobs SET status = 'cancel_requested', error = ?, updated_at = ? WHERE job_id = ?",
                (text, now, job_id),
            )
            self.connection.execute(
                "UPDATE processing_runs SET status = 'cancel_requested', error = ?, revision = revision + 1, updated_at = ? WHERE run_id = ?",
                (text, now, snapshot.run.run_id),
            )
        else:
            self.connection.execute(
                "UPDATE stage_runs SET status = 'cancelled', error = ?, updated_at = ?, completed_at = ? WHERE run_id = ? AND status IN ('pending', 'failed', 'stale')",
                (text, now, now, snapshot.run.run_id),
            )
            self._finish_job_run(job_id, snapshot.run.run_id, "cancelled", text, now)
        return self.get_snapshot(job_id)

    def retry(self, job_id: str) -> ProcessingSnapshot:
        snapshot = self.get_snapshot(job_id)
        if snapshot.job.status not in {"failed_retryable", "stale"}:
            raise ValueError("only retryable or stale jobs can be queued again")
        failed = next(
            (
                stage
                for stage in snapshot.stages
                if stage.status in {StageStatus.FAILED, StageStatus.STALE}
            ),
            None,
        )
        if failed is None:
            raise RuntimeError("retryable job has no failed stage")
        now = self.now()
        self.connection.execute(
            """
            UPDATE stage_runs SET status = 'pending', progress = 0.0, error = NULL,
                completed_at = NULL, updated_at = ? WHERE stage_run_id = ?
            """,
            (now, failed.stage_run_id),
        )
        self.connection.execute(
            """
            UPDATE processing_jobs SET status = 'queued', error = NULL,
                available_at = ?, lease_owner = NULL, heartbeat_at = NULL,
                completed_at = NULL, updated_at = ? WHERE job_id = ?
            """,
            (now, now, job_id),
        )
        self.connection.execute(
            """
            UPDATE processing_runs SET status = 'queued', current_stage = ?, error = NULL,
                revision = revision + 1, completed_at = NULL, updated_at = ? WHERE run_id = ?
            """,
            (failed.stage, now, snapshot.run.run_id),
        )
        return self.get_snapshot(job_id)

    def recover_expired(self) -> tuple[str, ...]:
        now = self.now()
        rows = self.connection.execute(
            "SELECT * FROM worker_leases WHERE status = 'active' AND expires_at <= ? ORDER BY expires_at",
            (now,),
        ).fetchall()
        recovered: list[str] = []
        for row in rows:
            job = self.connection.execute(
                "SELECT * FROM processing_jobs WHERE job_id = ?", (row["job_id"],)
            ).fetchone()
            attempt = self.connection.execute(
                "SELECT * FROM stage_attempts WHERE attempt_id = ?",
                (row["attempt_id"],),
            ).fetchone()
            if job is None or attempt is None:
                continue
            reason = f"worker lease expired: {row['worker_id']}"
            self.connection.execute(
                "UPDATE worker_leases SET status = 'lost', released_at = ? WHERE lease_id = ? AND status = 'active'",
                (now, row["lease_id"]),
            )
            self.connection.execute(
                "UPDATE stage_attempts SET status = 'lost_lease', error = ?, completed_at = ? WHERE attempt_id = ? AND status = 'running'",
                (reason, now, row["attempt_id"]),
            )
            self.connection.execute(
                "UPDATE stage_runs SET status = 'failed', error = ?, updated_at = ?, completed_at = ? WHERE stage_run_id = ? AND status = 'running'",
                (reason, now, now, attempt["stage_run_id"]),
            )
            self.connection.execute(
                "UPDATE processing_jobs SET status = 'failed_retryable', error = ?, lease_owner = NULL, updated_at = ? WHERE job_id = ?",
                (reason, now, row["job_id"]),
            )
            self.connection.execute(
                "UPDATE processing_runs SET status = 'failed_retryable', error = ?, revision = revision + 1, updated_at = ? WHERE run_id = ?",
                (reason, now, job["run_id"]),
            )
            recovered.append(str(row["job_id"]))
        return tuple(recovered)

    def _claim(self, lease_id: str, resume: dict[str, Any] | None) -> ProcessingClaim:
        lease_row = self.connection.execute(
            "SELECT * FROM worker_leases WHERE lease_id = ?", (lease_id,)
        ).fetchone()
        if lease_row is None:
            raise RuntimeError("new worker lease disappeared")
        job_row = self.connection.execute(
            "SELECT * FROM processing_jobs WHERE job_id = ?", (lease_row["job_id"],)
        ).fetchone()
        attempt_row = self.connection.execute(
            "SELECT * FROM stage_attempts WHERE attempt_id = ?",
            (lease_row["attempt_id"],),
        ).fetchone()
        if job_row is None or attempt_row is None:
            raise RuntimeError("new worker claim is incomplete")
        stage_row = self.connection.execute(
            "SELECT * FROM stage_runs WHERE stage_run_id = ?",
            (attempt_row["stage_run_id"],),
        ).fetchone()
        run_row = self.connection.execute(
            "SELECT * FROM processing_runs WHERE run_id = ?", (job_row["run_id"],)
        ).fetchone()
        if stage_row is None or run_row is None:
            raise RuntimeError("new worker claim graph is incomplete")
        return ProcessingClaim(
            job=_job(job_row),
            run=_run(run_row),
            stage=_stage(stage_row),
            attempt=_attempt(attempt_row),
            lease=_lease(lease_row),
            resume_checkpoint=resume,
        )

    def _validate_active(
        self, claim: ProcessingClaim, now: str, *, allow_cancel: bool = False
    ) -> None:
        row = self.connection.execute(
            """
            SELECT l.status AS lease_status, l.expires_at, j.status AS job_status
            FROM worker_leases l JOIN processing_jobs j ON j.job_id = l.job_id
            WHERE l.lease_id = ? AND l.lease_token = ? AND l.attempt_id = ?
            """,
            (claim.lease.lease_id, claim.lease.lease_token, claim.attempt.attempt_id),
        ).fetchone()
        if row is None or row["lease_status"] != "active" or row["expires_at"] <= now:
            raise RuntimeError("worker lease is no longer active")
        allowed = {"running"}
        if allow_cancel:
            allowed.add("cancel_requested")
        if row["job_status"] not in allowed:
            raise RuntimeError("processing job no longer belongs to this worker")

    def _release_lease(self, claim: ProcessingClaim, now: str, status: str) -> None:
        self.connection.execute(
            """
            UPDATE worker_leases SET status = ?, heartbeat_at = ?, released_at = ?
            WHERE lease_id = ? AND lease_token = ? AND status = 'active'
            """,
            (status, now, now, claim.lease.lease_id, claim.lease.lease_token),
        )

    def _advance(self, job_id: str, run_id: str, now: str) -> None:
        stages = self.connection.execute(
            "SELECT * FROM stage_runs WHERE run_id = ? ORDER BY ordinal", (run_id,)
        ).fetchall()
        pending = next((row for row in stages if row["status"] == "pending"), None)
        succeeded = sum(row["status"] == "succeeded" for row in stages)
        progress = float(succeeded) / float(len(stages))
        if pending is None:
            blocking = [
                row
                for row in stages
                if row["status"] != "succeeded"
                and not (bool(row["optional"]) and row["status"] == "failed")
            ]
            if blocking:
                raise RuntimeError(
                    "processing graph cannot advance past an incomplete required stage"
                )
            self.connection.execute(
                "UPDATE processing_jobs SET status = 'succeeded', result_json = ?, error = NULL, lease_owner = NULL, updated_at = ?, completed_at = ? WHERE job_id = ?",
                (_json({"status": "succeeded"}), now, now, job_id),
            )
            self.connection.execute(
                "UPDATE processing_runs SET status = 'succeeded', current_stage = 'completed', progress = 1.0, error = NULL, revision = revision + 1, updated_at = ?, completed_at = ? WHERE run_id = ?",
                (now, now, run_id),
            )
            return
        self.connection.execute(
            "UPDATE processing_jobs SET status = 'queued', lease_owner = NULL, heartbeat_at = NULL, updated_at = ? WHERE job_id = ?",
            (now, job_id),
        )
        self.connection.execute(
            "UPDATE processing_runs SET status = 'queued', current_stage = ?, progress = ?, revision = revision + 1, updated_at = ? WHERE run_id = ?",
            (pending["stage"], progress, now, run_id),
        )

    def _finish_job_run(
        self, job_id: str, run_id: str, status: str, error: str, now: str
    ) -> None:
        self.connection.execute(
            "UPDATE processing_jobs SET status = ?, error = ?, lease_owner = NULL, updated_at = ?, completed_at = ? WHERE job_id = ?",
            (status, error, now, now, job_id),
        )
        self.connection.execute(
            "UPDATE processing_runs SET status = ?, error = ?, revision = revision + 1, updated_at = ?, completed_at = ? WHERE run_id = ?",
            (status, error, now, now, run_id),
        )
