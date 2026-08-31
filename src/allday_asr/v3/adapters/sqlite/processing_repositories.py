from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.models import ProcessingRun, ProcessingStatus, RecordingSessionState
from allday_asr.v3.domain.processing import (
    AttemptStatus,
    BackupEvidence,
    LeaseStatus,
    ProcessingClaim,
    ProcessingJob,
    ProcessingSnapshot,
    SpeakerTrack,
    StageAttempt,
    StageRun,
    StageStatus,
    Utterance,
    WorkerLease,
)

from .repositories import Clock


_EFFECTIVE = (
    "created",
    "queued",
    "running",
    "waiting_review",
    "failed_retryable",
    "cancel_requested",
    "stale",
)


class SqliteDurableProcessingRepository:
    def __init__(self, connection: sqlite3.Connection, *, now: Clock) -> None:
        self.connection = connection
        self.now = now

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
                prior = [row for row in stages if int(row["ordinal"]) < int(stage_row["ordinal"])]
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
            (lease_id, selected_job["job_id"], attempt_id, worker_id, lease_token, now, now, expires),
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
            (selected_stage["stage"], float(completed) / float(total), now, selected_job["run_id"]),
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
            (stage for stage in snapshot.stages if stage.status in {StageStatus.FAILED, StageStatus.STALE}),
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
                "SELECT * FROM stage_attempts WHERE attempt_id = ?", (row["attempt_id"],)
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
            "SELECT * FROM stage_attempts WHERE attempt_id = ?", (lease_row["attempt_id"],)
        ).fetchone()
        if job_row is None or attempt_row is None:
            raise RuntimeError("new worker claim is incomplete")
        stage_row = self.connection.execute(
            "SELECT * FROM stage_runs WHERE stage_run_id = ?", (attempt_row["stage_run_id"],)
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
                row for row in stages
                if row["status"] != "succeeded" and not (bool(row["optional"]) and row["status"] == "failed")
            ]
            if blocking:
                raise RuntimeError("processing graph cannot advance past an incomplete required stage")
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


class SqliteAdmissionRepository:
    def __init__(self, connection: sqlite3.Connection, *, now: Clock) -> None:
        self.connection = connection
        self.now = now

    def add_evidence(self, evidence: BackupEvidence) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO backup_evidence (
                evidence_id, session_id, provider, storage_kind, digest, status,
                restore_checked_at, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING
            """,
            (
                evidence.evidence_id,
                evidence.session_id,
                evidence.provider,
                evidence.storage_kind,
                evidence.digest,
                evidence.status.value,
                _optional_datetime(evidence.restore_checked_at),
                _json(evidence.metadata),
                _datetime(evidence.created_at),
            ),
        )
        return cursor.rowcount == 1

    def evaluate(self, session_id: str) -> tuple[bool, str | None]:
        session = self.connection.execute(
            "SELECT state FROM recording_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if session is None:
            raise KeyError(f"recording session does not exist: {session_id}")
        manifest_count = int(self.connection.execute(
            "SELECT COUNT(*) FROM session_manifests WHERE session_id = ?", (session_id,)
        ).fetchone()[0])
        segments = self.connection.execute(
            """
            SELECT r.state FROM capture_segments s
            JOIN audio_replicas r ON r.replica_id = s.replica_id
            WHERE s.session_id = ?
            """,
            (session_id,),
        ).fetchall()
        evidence = self.connection.execute(
            """
            SELECT 1 FROM backup_evidence WHERE session_id = ? AND status = 'verified'
              AND restore_checked_at IS NOT NULL LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        if manifest_count != 1:
            return False, "session_manifest_missing"
        if not segments:
            return False, "capture_segments_missing"
        if any(row["state"] != "available" for row in segments):
            return False, "computer_audio_replica_unavailable"
        if evidence is None:
            return False, "backup_restore_evidence_required"
        return True, None

    def apply(self, session_id: str, admitted: bool, reason: str | None) -> int:
        now = self.now()
        row = self.connection.execute(
            "SELECT revision FROM recording_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"recording session does not exist: {session_id}")
        revision = int(row["revision"]) + 1
        self.connection.execute(
            """
            UPDATE recording_sessions SET state = ?, revision = ?, status_code = ?,
                current_stage = 'backup_admitted', progress = ?, blocking_reason = ?,
                updated_at = ? WHERE session_id = ?
            """,
            (
                RecordingSessionState.READY_FOR_PROCESSING.value if admitted else RecordingSessionState.ADMISSION_BLOCKED.value,
                revision,
                "ready" if admitted else "backup_required",
                1.0 if admitted else 0.0,
                None if admitted else reason,
                now,
                session_id,
            ),
        )
        return revision


class SqliteEvidenceProjectionRepository:
    def __init__(self, connection: sqlite3.Connection, *, now: Clock) -> None:
        self.connection = connection
        self.now = now

    def add_speaker_track(self, track: SpeakerTrack) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO speaker_tracks (
                speaker_track_id, session_id, run_id, label, source_artifact_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING
            """,
            (
                track.speaker_track_id,
                track.session_id,
                track.run_id,
                track.label,
                track.source_artifact_id,
                _datetime(track.created_at),
            ),
        )
        return cursor.rowcount == 1

    def add_utterance(self, utterance: Utterance) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO utterances (
                utterance_id, session_id, run_id, source_artifact_id,
                speaker_track_id, ordinal, start_ms, end_ms, text, evidence_json,
                revision, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                utterance.utterance_id,
                utterance.session_id,
                utterance.run_id,
                utterance.source_artifact_id,
                utterance.speaker_track_id,
                utterance.ordinal,
                utterance.start_ms,
                utterance.end_ms,
                utterance.text,
                _json(utterance.evidence),
                utterance.revision,
                utterance.status,
                _datetime(utterance.created_at),
                _datetime(utterance.updated_at),
            ),
        )
        return cursor.rowcount == 1

    def get_utterance(self, utterance_id: str) -> Utterance:
        row = self.connection.execute(
            "SELECT * FROM utterances WHERE utterance_id = ?", (utterance_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"utterance does not exist: {utterance_id}")
        return _utterance(row)

    def revise_utterance(
        self, utterance_id: str, expected_revision: int, text: str
    ) -> Utterance:
        now = self.now()
        cursor = self.connection.execute(
            """
            UPDATE utterances SET text = ?, revision = revision + 1,
                status = 'active', updated_at = ?
            WHERE utterance_id = ? AND revision = ?
            """,
            (text, now, utterance_id, expected_revision),
        )
        if cursor.rowcount != 1:
            raise ValueError("utterance revision conflict")
        return self.get_utterance(utterance_id)


def _job(row: sqlite3.Row) -> ProcessingJob:
    return ProcessingJob(
        job_id=str(row["job_id"]),
        run_id=str(row["run_id"]),
        kind=str(row["kind"]),
        status=str(row["status"]),
        priority=int(row["priority"]),
        request=_object(row["request_json"]),
        result=_optional_object(row["result_json"]),
        error=row["error"],
        available_at=_parse_datetime(row["available_at"]),
        lease_owner=row["lease_owner"],
        heartbeat_at=_optional_parse_datetime(row["heartbeat_at"]),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
        completed_at=_optional_parse_datetime(row["completed_at"]),
    )


def _run(row: sqlite3.Row) -> ProcessingRun:
    return ProcessingRun(
        run_id=str(row["run_id"]),
        session_id=str(row["session_id"]),
        pipeline_version=str(row["pipeline_version"]),
        input_revision=int(row["input_revision"]),
        status=ProcessingStatus(str(row["status"])),
        config_digest=str(row["config_digest"]),
        current_stage=row["current_stage"],
        progress=float(row["progress"]),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
        completed_at=_optional_parse_datetime(row["completed_at"]),
        error=row["error"],
        legacy_ref=row["legacy_ref"],
        revision=int(row["revision"]),
    )


def _stage(row: sqlite3.Row) -> StageRun:
    return StageRun(
        stage_run_id=str(row["stage_run_id"]),
        run_id=str(row["run_id"]),
        stage=str(row["stage"]),
        ordinal=int(row["ordinal"]),
        optional=bool(row["optional"]),
        status=StageStatus(str(row["status"])),
        progress=float(row["progress"]),
        output=_optional_object(row["output_json"]),
        error=row["error"],
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
        completed_at=_optional_parse_datetime(row["completed_at"]),
    )


def _attempt(row: sqlite3.Row) -> StageAttempt:
    return StageAttempt(
        attempt_id=str(row["attempt_id"]),
        stage_run_id=str(row["stage_run_id"]),
        attempt_number=int(row["attempt_number"]),
        status=AttemptStatus(str(row["status"])),
        worker_id=str(row["worker_id"]),
        config=_object(row["config_json"]),
        checkpoint=_optional_object(row["checkpoint_json"]),
        log_summary=row["log_summary"],
        error=row["error"],
        started_at=_parse_datetime(row["started_at"]),
        heartbeat_at=_parse_datetime(row["heartbeat_at"]),
        completed_at=_optional_parse_datetime(row["completed_at"]),
    )


def _lease(row: sqlite3.Row) -> WorkerLease:
    return WorkerLease(
        lease_id=str(row["lease_id"]),
        job_id=str(row["job_id"]),
        attempt_id=str(row["attempt_id"]),
        worker_id=str(row["worker_id"]),
        lease_token=str(row["lease_token"]),
        status=LeaseStatus(str(row["status"])),
        acquired_at=_parse_datetime(row["acquired_at"]),
        heartbeat_at=_parse_datetime(row["heartbeat_at"]),
        expires_at=_parse_datetime(row["expires_at"]),
        released_at=_optional_parse_datetime(row["released_at"]),
    )


def _utterance(row: sqlite3.Row) -> Utterance:
    return Utterance(
        utterance_id=str(row["utterance_id"]),
        session_id=str(row["session_id"]),
        run_id=str(row["run_id"]),
        source_artifact_id=str(row["source_artifact_id"]),
        speaker_track_id=row["speaker_track_id"],
        ordinal=int(row["ordinal"]),
        start_ms=int(row["start_ms"]),
        end_ms=int(row["end_ms"]),
        text=str(row["text"]),
        evidence=_object(row["evidence_json"]),
        revision=int(row["revision"]),
        status=str(row["status"]),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _optional_datetime(value: datetime | None) -> str | None:
    return _datetime(value) if value is not None else None


def _parse_datetime(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("stored timestamp is not timezone-aware")
    return parsed.astimezone(timezone.utc)


def _optional_parse_datetime(value: object) -> datetime | None:
    return _parse_datetime(value) if value is not None else None


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _optional_json(value: object | None) -> str | None:
    return _json(value) if value is not None else None


def _object(value: object) -> dict[str, Any]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise ValueError("stored JSON is not an object")
    return parsed


def _optional_object(value: object) -> dict[str, Any] | None:
    return _object(value) if value is not None else None


__all__ = [
    "SqliteAdmissionRepository",
    "SqliteDurableProcessingRepository",
    "SqliteEvidenceProjectionRepository",
]
