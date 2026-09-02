from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any
from allday_asr.v3.domain.models import ProcessingRun, ProcessingStatus
from allday_asr.v3.domain.processing import (
    AttemptStatus,
    LeaseStatus,
    ProcessingJob,
    StageAttempt,
    StageRun,
    StageStatus,
    Utterance,
    WorkerLease,
)


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
    from allday_asr.v3.domain.identity import SelfIdentity

    return Utterance(
        utterance_id=str(row["utterance_id"]),
        session_id=str(row["session_id"]),
        run_id=str(row["run_id"]),
        source_artifact_id=str(row["source_artifact_id"]),
        speaker_track_id=row["speaker_track_id"],
        original_speaker_track_id=row["original_speaker_track_id"],
        ordinal=int(row["ordinal"]),
        start_ms=int(row["start_ms"]),
        end_ms=int(row["end_ms"]),
        start_at=_parse_datetime(row["start_at"]),
        end_at=_parse_datetime(row["end_at"]),
        text=str(row["text"]),
        original_text=str(row["original_text"]),
        identity=SelfIdentity(str(row["identity"])),
        original_identity=SelfIdentity(str(row["original_identity"])),
        identity_evidence=_object(row["identity_evidence_json"]),
        evidence=_object(row["evidence_json"]),
        revision=int(row["revision"]),
        status=str(row["status"]),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


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
