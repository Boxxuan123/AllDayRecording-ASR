from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any
from allday_asr.v3.domain.device_sync import (
    ClientOperation,
    ClientOperationRecord,
    ClientOperationStatus,
    DeviceCredential,
    DeviceScope,
    OperationReceipt,
)
from allday_asr.v3.domain.models import (
    Artifact,
    AudioAsset,
    AudioFormat,
    ChangeEvent,
    ChangeOperation,
    ProcessingRun,
    ProcessingStatus,
    RecordingSession,
    RecordingSessionState,
)


def _recording_session(row: sqlite3.Row) -> RecordingSession:
    return RecordingSession(
        session_id=str(row["session_id"]),
        captured_start=_parse_datetime(row["captured_start"]),
        captured_end=_optional_parse_datetime(row["captured_end"]),
        timezone=str(row["timezone"]),
        state=RecordingSessionState(str(row["state"])),
        revision=int(row["revision"]),
        status_code=str(row["status_code"]),
        current_stage=row["current_stage"],
        progress=float(row["progress"]),
        blocking_reason=row["blocking_reason"],
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
        legacy_ref=row["legacy_ref"],
    )


def _audio_asset(row: sqlite3.Row) -> AudioAsset:
    return AudioAsset(
        asset_id=str(row["asset_id"]),
        sha256=str(row["sha256"]),
        size_bytes=int(row["size_bytes"]),
        duration_ms=int(row["duration_ms"]),
        format=AudioFormat(str(row["format"])),
        media_id=str(row["media_id"]),
        created_at=_parse_datetime(row["created_at"]),
        legacy_ref=row["legacy_ref"],
    )


def _processing_run(row: sqlite3.Row) -> ProcessingRun:
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


def _artifact(row: sqlite3.Row) -> Artifact:
    input_refs = json.loads(str(row["input_refs_json"]))
    metadata = json.loads(str(row["metadata_json"]))
    if not isinstance(input_refs, list) or not isinstance(metadata, dict):
        raise ValueError("stored artifact JSON is invalid")
    return Artifact(
        artifact_id=str(row["artifact_id"]),
        run_id=row["run_id"],
        kind=str(row["kind"]),
        producer=str(row["producer"]),
        producer_version=str(row["producer_version"]),
        config_digest=str(row["config_digest"]),
        input_refs=tuple(str(value) for value in input_refs),
        storage_ref=str(row["storage_ref"]),
        sha256=row["sha256"],
        size_bytes=(int(row["size_bytes"]) if row["size_bytes"] is not None else None),
        status=str(row["status"]),
        metadata=metadata,
        legacy_ref=row["legacy_ref"],
        created_at=_parse_datetime(row["created_at"]),
    )


def _change_event(row: sqlite3.Row) -> ChangeEvent:
    payload = row["payload_json"]
    return ChangeEvent(
        sequence=int(row["sequence"]),
        resource_type=str(row["resource_type"]),
        resource_id=str(row["resource_id"]),
        revision=int(row["revision"]),
        operation=ChangeOperation(str(row["operation"])),
        payload=json.loads(str(payload)) if payload is not None else None,
        created_at=_parse_datetime(row["created_at"]),
    )


def _device_credential(row: sqlite3.Row) -> DeviceCredential:
    scopes = json.loads(str(row["scopes_json"]))
    if not isinstance(scopes, list):
        raise ValueError("stored device scopes are invalid")
    return DeviceCredential(
        credential_id=str(row["credential_id"]),
        device_id=str(row["device_id"]),
        key_id=str(row["key_id"]),
        algorithm=str(row["algorithm"]),
        public_key=str(row["public_key"]),
        scopes=tuple(DeviceScope(str(value)) for value in scopes),
        passkey_credential_ref=str(row["passkey_credential_ref"]),
        created_at=_parse_datetime(row["created_at"]),
        last_used_at=_optional_parse_datetime(row["last_used_at"]),
        revoked_at=_optional_parse_datetime(row["revoked_at"]),
    )


def _client_operation_record(row: sqlite3.Row) -> ClientOperationRecord:
    payload = json.loads(str(row["payload_json"]))
    receipt = json.loads(str(row["receipt_json"]))
    if not isinstance(payload, dict) or not isinstance(receipt, dict):
        raise ValueError("stored client operation JSON is invalid")
    return ClientOperationRecord(
        device_id=str(row["device_id"]),
        operation=ClientOperation(
            operation_id=str(row["operation_id"]),
            kind=str(row["kind"]),
            base_revision=(
                int(row["base_revision"]) if row["base_revision"] is not None else None
            ),
            payload=payload,
        ),
        payload_sha256=str(row["payload_sha256"]),
        receipt=OperationReceipt(
            operation_id=str(receipt["operation_id"]),
            status=ClientOperationStatus(str(receipt["status"])),
            resource_revision=(
                int(receipt["resource_revision"])
                if receipt.get("resource_revision") is not None
                else None
            ),
            error=receipt.get("error"),
        ),
        created_at=_parse_datetime(row["created_at"]),
        completed_at=_parse_datetime(row["completed_at"]),
    )


def _datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("database timestamps must be timezone-aware")
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


def _json_object(value: object) -> dict[str, Any]:
    decoded = json.loads(str(value))
    if not isinstance(decoded, dict):
        raise ValueError("stored JSON value is not an object")
    return decoded
