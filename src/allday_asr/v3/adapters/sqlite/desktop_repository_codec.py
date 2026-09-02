from __future__ import annotations
import json
import sqlite3
from typing import Any
from allday_asr.v3.contracts import validate_utterance_dto


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
    return validate_utterance_dto(
        {
            "utterance_id": str(row["utterance_id"]),
            "session_id": str(row["session_id"]),
            "speaker_track_id": row["speaker_track_id"],
            "speaker_label": row["speaker_label"],
            "original_speaker_track_id": row["original_speaker_track_id"],
            "original_speaker_label": row["original_speaker_label"],
            "identity": str(row["identity"]),
            "original_identity": str(row["original_identity"]),
            "identity_evidence": _json_object(row["identity_evidence_json"]),
            "start_ms": int(row["start_ms"]),
            "end_ms": int(row["end_ms"]),
            "start_at": str(row["start_at"]),
            "end_at": str(row["end_at"]),
            "text": str(row["text"]),
            "original_text": str(row["original_text"]),
            "revision": int(row["revision"]),
            "status": str(row["status"]),
            "evidence": _json_object(row["evidence_json"]),
        }
    )


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
