from __future__ import annotations

import json
import sqlite3
from typing import Any

from allday_asr.v3.contracts import validate_utterance_dto


class SqliteDesktopReadRepository:
    """Read-only product projections for the loopback Desktop API."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def overview(self) -> dict[str, Any]:
        counts = self.connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM recording_sessions) AS sessions,
              (SELECT COUNT(*) FROM processing_jobs WHERE status IN
                ('queued', 'running', 'cancel_requested')) AS active_jobs,
              (SELECT COUNT(*) FROM processing_jobs WHERE status =
                'failed_retryable') AS retryable_jobs,
              (SELECT COUNT(*) FROM devices WHERE status = 'active') AS active_devices,
              (SELECT COALESCE(SUM(size_bytes), 0) FROM audio_assets) AS audio_bytes,
              (SELECT COUNT(DISTINCT session_id) FROM backup_evidence
                WHERE status = 'verified') AS backed_up_sessions
            """
        ).fetchone()
        recent = self.list_sessions(None, None, 6)
        jobs = self.list_processing_jobs(None, 6)
        return {
            "counts": {key: int(counts[key]) for key in counts.keys()},
            "recent_sessions": list(recent),
            "active_jobs": [
                value
                for value in jobs
                if value["status"] in {"queued", "running", "cancel_requested"}
            ],
        }

    def list_sessions(
        self,
        before_captured_start: str | None,
        before_session_id: str | None,
        limit: int,
    ) -> tuple[dict[str, Any], ...]:
        where = ""
        parameters: list[object] = []
        if before_captured_start is not None and before_session_id is not None:
            where = (
                "WHERE s.captured_start < ? OR "
                "(s.captured_start = ? AND s.session_id > ?)"
            )
            parameters.extend(
                [before_captured_start, before_captured_start, before_session_id]
            )
        parameters.append(limit)
        rows = self.connection.execute(
            f"""
            SELECT s.*,
              COUNT(DISTINCT seg.segment_id) AS segment_count,
              COALESCE(MAX(seg.session_end_ms), 0) AS duration_ms,
              (
                SELECT a.media_id FROM capture_segments first_seg
                JOIN audio_assets a ON a.asset_id = first_seg.asset_id
                WHERE first_seg.session_id = s.session_id
                ORDER BY first_seg.sequence, first_seg.segment_id LIMIT 1
              ) AS media_id,
              (
                SELECT p.run_id FROM processing_runs p
                WHERE p.session_id = s.session_id
                ORDER BY p.created_at DESC, p.run_id DESC LIMIT 1
              ) AS latest_run_id,
              (
                SELECT p.status FROM processing_runs p
                WHERE p.session_id = s.session_id
                ORDER BY p.created_at DESC, p.run_id DESC LIMIT 1
              ) AS processing_status
            FROM recording_sessions s
            LEFT JOIN capture_segments seg ON seg.session_id = s.session_id
            {where}
            GROUP BY s.session_id
            ORDER BY s.captured_start DESC, s.session_id
            LIMIT ?
            """,
            parameters,
        ).fetchall()
        return tuple(_session(row) for row in rows)

    def session_detail(self, session_id: str) -> dict[str, Any]:
        session_rows = self.connection.execute(
            "SELECT captured_start, session_id FROM recording_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if session_rows is None:
            raise KeyError(f"recording session does not exist: {session_id}")
        # Read the selected row directly; keyset pagination intentionally excludes it.
        row = self.connection.execute(
            """
            SELECT s.*,
              COUNT(DISTINCT seg.segment_id) AS segment_count,
              COALESCE(MAX(seg.session_end_ms), 0) AS duration_ms,
              (SELECT a.media_id FROM capture_segments x JOIN audio_assets a
                ON a.asset_id = x.asset_id WHERE x.session_id = s.session_id
                ORDER BY x.sequence, x.segment_id LIMIT 1) AS media_id,
              (SELECT p.run_id FROM processing_runs p WHERE p.session_id = s.session_id
                ORDER BY p.created_at DESC, p.run_id DESC LIMIT 1) AS latest_run_id,
              (SELECT p.status FROM processing_runs p WHERE p.session_id = s.session_id
                ORDER BY p.created_at DESC, p.run_id DESC LIMIT 1) AS processing_status
            FROM recording_sessions s
            LEFT JOIN capture_segments seg ON seg.session_id = s.session_id
            WHERE s.session_id = ? GROUP BY s.session_id
            """,
            (session_id,),
        ).fetchone()
        assert row is not None
        segments = self.connection.execute(
            """
            SELECT seg.segment_id, seg.sequence, seg.session_start_ms,
              seg.session_end_ms, a.asset_id, a.media_id, a.sha256,
              a.size_bytes, a.duration_ms, a.format, r.replica_id,
              r.state AS replica_state, d.name AS device_name
            FROM capture_segments seg
            JOIN audio_assets a ON a.asset_id = seg.asset_id
            JOIN audio_replicas r ON r.replica_id = seg.replica_id
            LEFT JOIN devices d ON d.device_id = r.device_id
            WHERE seg.session_id = ? ORDER BY seg.sequence, seg.segment_id
            """,
            (session_id,),
        ).fetchall()
        runs = self.connection.execute(
            """
            SELECT p.*, j.job_id, j.status AS job_status
            FROM processing_runs p LEFT JOIN processing_jobs j ON j.run_id = p.run_id
            WHERE p.session_id = ? ORDER BY p.created_at DESC, p.run_id DESC
            """,
            (session_id,),
        ).fetchall()
        utterances = self.connection.execute(
            """
            SELECT u.*, t.label AS speaker_label FROM utterances u
            LEFT JOIN speaker_tracks t ON t.speaker_track_id = u.speaker_track_id
            WHERE u.session_id = ? ORDER BY u.start_ms, u.end_ms, u.utterance_id
            LIMIT 500
            """,
            (session_id,),
        ).fetchall()
        artifacts = self.connection.execute(
            """
            SELECT a.*, CASE WHEN EXISTS (
              SELECT 1 FROM artifact_status_events e WHERE e.artifact_id = a.artifact_id
            ) THEN 'stale' ELSE a.status END AS effective_status
            FROM artifacts a JOIN processing_runs p ON p.run_id = a.run_id
            WHERE p.session_id = ? ORDER BY a.created_at DESC, a.artifact_id
            """,
            (session_id,),
        ).fetchall()
        backups = self.connection.execute(
            "SELECT * FROM backup_evidence WHERE session_id = ? ORDER BY created_at DESC",
            (session_id,),
        ).fetchall()
        return {
            "session": _session(row),
            "segments": [_dict(value) for value in segments],
            "runs": [_run(value) for value in runs],
            "utterances": [_utterance(value) for value in utterances],
            "artifacts": [_artifact(value) for value in artifacts],
            "backups": [_backup(value) for value in backups],
        }

    def list_processing_jobs(
        self, status: str | None, limit: int
    ) -> tuple[dict[str, Any], ...]:
        where = "WHERE j.status = ?" if status else ""
        parameters: tuple[object, ...] = (status, limit) if status else (limit,)
        rows = self.connection.execute(
            f"""
            SELECT j.*, p.session_id, p.pipeline_version, p.input_revision,
              p.revision, p.current_stage, p.progress,
              (SELECT COUNT(*) FROM stage_runs s WHERE s.run_id = p.run_id) AS stage_count,
              (SELECT COUNT(*) FROM stage_runs s WHERE s.run_id = p.run_id
                AND s.status = 'succeeded') AS completed_stage_count
            FROM processing_jobs j JOIN processing_runs p ON p.run_id = j.run_id
            {where} ORDER BY j.created_at DESC, j.job_id DESC LIMIT ?
            """,
            parameters,
        ).fetchall()
        return tuple(_job(row) for row in rows)

    def list_reviews(self, limit: int) -> tuple[dict[str, Any], ...]:
        rows = self.connection.execute(
            """
            SELECT p.run_id, p.session_id, s.stage_run_id, s.stage, s.status,
              s.error, s.updated_at
            FROM stage_runs s JOIN processing_runs p ON p.run_id = s.run_id
            WHERE s.status = 'waiting_review'
            ORDER BY s.updated_at, s.stage_run_id LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return tuple(_dict(row) for row in rows)

    def list_devices(self) -> tuple[dict[str, Any], ...]:
        rows = self.connection.execute(
            """
            SELECT d.*,
              EXISTS(SELECT 1 FROM device_credentials c
                WHERE c.device_id = d.device_id AND c.revoked_at IS NULL) AS paired,
              (SELECT receiver_id FROM pairing_records p
                WHERE p.device_id = d.device_id ORDER BY paired_at DESC LIMIT 1) AS receiver_id
            FROM devices d ORDER BY d.kind, d.name, d.device_id
            """
        ).fetchall()
        return tuple(_device(row) for row in rows)

    def data_health(self) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM audio_assets) AS audio_assets,
              (SELECT COALESCE(SUM(size_bytes), 0) FROM audio_assets) AS audio_bytes,
              (SELECT COUNT(*) FROM audio_replicas WHERE state = 'available') AS available_replicas,
              (SELECT COUNT(*) FROM audio_replicas
                WHERE state IN ('failed_retryable', 'quarantined')) AS unhealthy_replicas,
              (SELECT COUNT(*) FROM backup_evidence WHERE status = 'verified') AS verified_backups,
              (SELECT COUNT(*) FROM artifacts) AS artifacts,
              (SELECT COALESCE(SUM(size_bytes), 0) FROM artifacts) AS artifact_bytes,
              (SELECT COUNT(*) FROM artifact_status_events WHERE status = 'stale') AS stale_artifacts
            """
        ).fetchone()
        return {key: int(row[key]) for key in row.keys()}

    def media(self, media_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT a.media_id, a.size_bytes, a.format, r.storage_key
            FROM audio_assets a JOIN audio_replicas r ON r.asset_id = a.asset_id
            WHERE a.media_id = ? AND r.state = 'available'
            ORDER BY r.verified_at DESC, r.replica_id LIMIT 1
            """,
            (media_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"media does not exist: {media_id}")
        return _dict(row)

    def processing_events(
        self, after_sequence: int, limit: int
    ) -> tuple[dict[str, Any], ...]:
        rows = self.connection.execute(
            """
            SELECT sequence, resource_id, revision, operation, payload_json, created_at
            FROM change_events WHERE sequence > ? AND resource_type = 'processing_run'
            ORDER BY sequence LIMIT ?
            """,
            (after_sequence, limit),
        ).fetchall()
        return tuple(
            {
                **_dict(row),
                "payload": _json_object(row["payload_json"]),
            }
            for row in rows
        )


def _session(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "session_id": str(row["session_id"]),
        "captured_start": str(row["captured_start"]),
        "captured_end": row["captured_end"],
        "timezone": str(row["timezone"]),
        "state": str(row["state"]),
        "revision": int(row["revision"]),
        "status_code": str(row["status_code"]),
        "current_stage": row["current_stage"],
        "progress": float(row["progress"]),
        "updated_at": str(row["updated_at"]),
        "blocking_reason": row["blocking_reason"],
        "legacy_ref": row["legacy_ref"],
        "segment_count": int(row["segment_count"]),
        "duration_ms": int(row["duration_ms"]),
        "media_id": row["media_id"],
        "latest_run_id": row["latest_run_id"],
        "processing_status": row["processing_status"],
    }


def _run(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "run_id": str(row["run_id"]),
        "job_id": row["job_id"],
        "session_id": str(row["session_id"]),
        "pipeline_version": str(row["pipeline_version"]),
        "input_revision": int(row["input_revision"]),
        "revision": int(row["revision"]),
        "status": str(row["status"]),
        "job_status": row["job_status"],
        "current_stage": row["current_stage"],
        "progress": float(row["progress"]),
        "error": row["error"],
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "completed_at": row["completed_at"],
    }


def _utterance(row: sqlite3.Row) -> dict[str, Any]:
    return validate_utterance_dto({
        "utterance_id": str(row["utterance_id"]),
        "session_id": str(row["session_id"]),
        "speaker_track_id": row["speaker_track_id"],
        "speaker_label": row["speaker_label"],
        "start_ms": int(row["start_ms"]),
        "end_ms": int(row["end_ms"]),
        "text": str(row["text"]),
        "revision": int(row["revision"]),
        "status": str(row["status"]),
        "evidence": _json_object(row["evidence_json"]),
    })


def _artifact(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "artifact_id": str(row["artifact_id"]),
        "run_id": str(row["run_id"]),
        "kind": str(row["kind"]),
        "producer": str(row["producer"]),
        "producer_version": str(row["producer_version"]),
        "status": str(row["effective_status"]),
        "sha256": str(row["sha256"]) if row["sha256"] is not None else None,
        "size_bytes": (
            int(row["size_bytes"]) if row["size_bytes"] is not None else None
        ),
        "metadata": _json_object(row["metadata_json"]),
        "created_at": str(row["created_at"]),
    }


def _backup(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "evidence_id": str(row["evidence_id"]),
        "provider": str(row["provider"]),
        "storage_kind": str(row["storage_kind"]),
        "digest": str(row["digest"]),
        "status": str(row["status"]),
        "restore_checked_at": row["restore_checked_at"],
        "metadata": _json_object(row["metadata_json"]),
        "created_at": str(row["created_at"]),
    }


def _job(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "job_id": str(row["job_id"]),
        "run_id": str(row["run_id"]),
        "session_id": str(row["session_id"]),
        "pipeline_version": str(row["pipeline_version"]),
        "input_revision": int(row["input_revision"]),
        "revision": int(row["revision"]),
        "status": str(row["status"]),
        "priority": int(row["priority"]),
        "current_stage": row["current_stage"],
        "progress": float(row["progress"]),
        "stage_count": int(row["stage_count"]),
        "completed_stage_count": int(row["completed_stage_count"]),
        "error": row["error"],
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "completed_at": row["completed_at"],
    }


def _device(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "device_id": str(row["device_id"]),
        "kind": str(row["kind"]),
        "name": str(row["name"]),
        "status": str(row["status"]),
        "revision": int(row["revision"]),
        "last_seen_at": row["last_seen_at"],
        "updated_at": str(row["updated_at"]),
        "paired": bool(row["paired"]),
        "receiver_id": row["receiver_id"],
    }


def _dict(row: sqlite3.Row) -> dict[str, Any]:
    return {str(key): row[key] for key in row.keys()}


def _json_object(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    parsed = json.loads(str(value))
    return parsed if isinstance(parsed, dict) else {}


__all__ = ["SqliteDesktopReadRepository"]
