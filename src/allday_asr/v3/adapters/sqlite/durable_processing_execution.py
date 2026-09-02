from __future__ import annotations
import secrets
import sqlite3
from datetime import timedelta
from typing import Any
from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.processing import (
    ProcessingClaim,
    ProcessingSnapshot,
)
from .processing_repository_codec import (
    _datetime,
    _parse_datetime,
    _json,
    _optional_json,
    _optional_object,
)


class DurableProcessingExecutionMixin:
    def claim_next(
        self, worker_id: str, lease_seconds: int, config: dict[str, Any]
    ) -> ProcessingClaim | None:
        if not worker_id or lease_seconds < 1:
            raise ValueError("worker id and lease duration are required")
        now = self.now()
        jobs = self.connection.execute(
            """
            SELECT * FROM processing_jobs
            WHERE status = 'queued' AND available_at <= ?
            ORDER BY priority DESC, created_at, job_id
            """,
            (now,),
        ).fetchall()
        selected_job: sqlite3.Row | None = None
        selected_stage: sqlite3.Row | None = None
        for job_row in jobs:
            stages = self.connection.execute(
                "SELECT * FROM stage_runs WHERE run_id = ? ORDER BY ordinal",
                (job_row["run_id"],),
            ).fetchall()
            for stage_row in stages:
                if stage_row["status"] != "pending":
                    continue
                prior = [
                    row
                    for row in stages
                    if int(row["ordinal"]) < int(stage_row["ordinal"])
                ]
                if all(
                    row["status"] == "succeeded"
                    or (bool(row["optional"]) and row["status"] == "failed")
                    for row in prior
                ):
                    selected_job = job_row
                    selected_stage = stage_row
                    break
            if selected_stage is not None:
                break
        if selected_job is None or selected_stage is None:
            return None

        previous = self.connection.execute(
            """
            SELECT attempt_number, checkpoint_json FROM stage_attempts
            WHERE stage_run_id = ? ORDER BY attempt_number DESC LIMIT 1
            """,
            (selected_stage["stage_run_id"],),
        ).fetchone()
        attempt_number = int(previous["attempt_number"]) + 1 if previous else 1
        resume = _optional_object(previous["checkpoint_json"]) if previous else None
        attempt_id = new_ulid()
        lease_id = new_ulid()
        lease_token = secrets.token_urlsafe(32)
        expires = _datetime(_parse_datetime(now) + timedelta(seconds=lease_seconds))
        self.connection.execute(
            """
            UPDATE processing_jobs SET status = 'running', lease_owner = ?,
                heartbeat_at = ?, updated_at = ?
            WHERE job_id = ? AND status = 'queued'
            """,
            (worker_id, now, now, selected_job["job_id"]),
        )
        self.connection.execute(
            """
            UPDATE stage_runs SET status = 'running', updated_at = ?
            WHERE stage_run_id = ? AND status = 'pending'
            """,
            (now, selected_stage["stage_run_id"]),
        )
        self.connection.execute(
            """
            INSERT INTO stage_attempts (
                attempt_id, stage_run_id, attempt_number, status, worker_id,
                config_json, checkpoint_json, started_at, heartbeat_at
            ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                selected_stage["stage_run_id"],
                attempt_number,
                worker_id,
                _json(config),
                _optional_json(resume),
                now,
                now,
            ),
        )
        self.connection.execute(
            """
            INSERT INTO worker_leases (
                lease_id, job_id, attempt_id, worker_id, lease_token, status,
                acquired_at, heartbeat_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            (
                lease_id,
                selected_job["job_id"],
                attempt_id,
                worker_id,
                lease_token,
                now,
                now,
                expires,
            ),
        )
        completed = self.connection.execute(
            "SELECT COUNT(*) FROM stage_runs WHERE run_id = ? AND status = 'succeeded'",
            (selected_job["run_id"],),
        ).fetchone()[0]
        total = self.connection.execute(
            "SELECT COUNT(*) FROM stage_runs WHERE run_id = ?",
            (selected_job["run_id"],),
        ).fetchone()[0]
        self.connection.execute(
            """
            UPDATE processing_runs SET status = 'running', current_stage = ?,
                progress = ?, revision = revision + 1, updated_at = ? WHERE run_id = ?
            """,
            (
                selected_stage["stage"],
                float(completed) / float(total),
                now,
                selected_job["run_id"],
            ),
        )
        return self._claim(lease_id, resume)

    def heartbeat(
        self,
        claim: ProcessingClaim,
        lease_seconds: int,
        checkpoint: dict[str, Any] | None,
    ) -> bool:
        now = self.now()
        self._validate_active(claim, now, allow_cancel=True)
        expires = _datetime(_parse_datetime(now) + timedelta(seconds=lease_seconds))
        self.connection.execute(
            """
            UPDATE worker_leases SET heartbeat_at = ?, expires_at = ?
            WHERE lease_id = ? AND lease_token = ? AND status = 'active'
            """,
            (now, expires, claim.lease.lease_id, claim.lease.lease_token),
        )
        self.connection.execute(
            """
            UPDATE stage_attempts SET heartbeat_at = ?,
                checkpoint_json = COALESCE(?, checkpoint_json)
            WHERE attempt_id = ? AND status = 'running'
            """,
            (now, _optional_json(checkpoint), claim.attempt.attempt_id),
        )
        self.connection.execute(
            "UPDATE processing_jobs SET heartbeat_at = ?, updated_at = ? WHERE job_id = ?",
            (now, now, claim.job.job_id),
        )
        status = self.connection.execute(
            "SELECT status FROM processing_jobs WHERE job_id = ?", (claim.job.job_id,)
        ).fetchone()
        return status is not None and status["status"] == "cancel_requested"

    def complete_stage(
        self,
        claim: ProcessingClaim,
        checkpoint: dict[str, Any],
        log_summary: str,
        output: dict[str, Any],
    ) -> ProcessingSnapshot:
        now = self.now()
        self._validate_active(claim, now)
        self.connection.execute(
            """
            UPDATE stage_attempts SET status = 'succeeded', checkpoint_json = ?,
                log_summary = ?, heartbeat_at = ?, completed_at = ?
            WHERE attempt_id = ? AND status = 'running'
            """,
            (_json(checkpoint), log_summary, now, now, claim.attempt.attempt_id),
        )
        self._release_lease(claim, now, "released")
        self.connection.execute(
            """
            UPDATE stage_runs SET status = 'succeeded', progress = 1.0,
                output_json = ?, error = NULL, updated_at = ?, completed_at = ?
            WHERE stage_run_id = ? AND status = 'running'
            """,
            (_json(output), now, now, claim.stage.stage_run_id),
        )
        self._advance(claim.job.job_id, claim.run.run_id, now)
        return self.get_snapshot(claim.job.job_id)

    def fail_stage(
        self,
        claim: ProcessingClaim,
        error: str,
        log_summary: str,
        retryable: bool,
    ) -> ProcessingSnapshot:
        now = self.now()
        self._validate_active(claim, now)
        text = error[:4000]
        self.connection.execute(
            """
            UPDATE stage_attempts SET status = 'failed', log_summary = ?, error = ?,
                heartbeat_at = ?, completed_at = ?
            WHERE attempt_id = ? AND status = 'running'
            """,
            (log_summary, text, now, now, claim.attempt.attempt_id),
        )
        self._release_lease(claim, now, "released")
        self.connection.execute(
            """
            UPDATE stage_runs SET status = 'failed', error = ?, updated_at = ?, completed_at = ?
            WHERE stage_run_id = ? AND status = 'running'
            """,
            (text, now, now, claim.stage.stage_run_id),
        )
        if claim.stage.optional:
            self._advance(claim.job.job_id, claim.run.run_id, now)
        else:
            status = "failed_retryable" if retryable else "failed_final"
            self.connection.execute(
                """
                UPDATE processing_jobs SET status = ?, lease_owner = NULL,
                    error = ?, updated_at = ?, completed_at = ? WHERE job_id = ?
                """,
                (status, text, now, None if retryable else now, claim.job.job_id),
            )
            self.connection.execute(
                """
                UPDATE processing_runs SET status = ?, error = ?, revision = revision + 1,
                    updated_at = ?, completed_at = ?
                WHERE run_id = ?
                """,
                (status, text, now, None if retryable else now, claim.run.run_id),
            )
        return self.get_snapshot(claim.job.job_id)

    def cancel_claim(self, claim: ProcessingClaim, reason: str) -> ProcessingSnapshot:
        now = self.now()
        self._validate_active(claim, now, allow_cancel=True)
        text = reason[:4000]
        self.connection.execute(
            """
            UPDATE stage_attempts SET status = 'cancelled', error = ?, heartbeat_at = ?,
                completed_at = ? WHERE attempt_id = ? AND status = 'running'
            """,
            (text, now, now, claim.attempt.attempt_id),
        )
        self._release_lease(claim, now, "released")
        self.connection.execute(
            "UPDATE stage_runs SET status = 'cancelled', error = ?, updated_at = ?, completed_at = ? WHERE stage_run_id = ?",
            (text, now, now, claim.stage.stage_run_id),
        )
        self.connection.execute(
            "UPDATE stage_runs SET status = 'cancelled', error = ?, updated_at = ?, completed_at = ? WHERE run_id = ? AND status = 'pending'",
            (text, now, now, claim.run.run_id),
        )
        self._finish_job_run(claim.job.job_id, claim.run.run_id, "cancelled", text, now)
        return self.get_snapshot(claim.job.job_id)
