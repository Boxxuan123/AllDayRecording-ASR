from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class DeviceKind(StrEnum):
    WATCH = "watch"
    PHONE = "phone"
    COMPUTER = "computer"


class DeviceStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class RecordingSessionState(StrEnum):
    CAPTURING = "capturing"
    CLOSING = "closing"
    RECOVERING = "recovering"
    QUARANTINED = "quarantined"
    SEALED = "sealed"
    PHONE_VERIFIED = "phone_verified"
    COMPUTER_INGESTED = "computer_ingested"
    ADMISSION_PENDING = "admission_pending"
    ADMISSION_BLOCKED = "admission_blocked"
    READY_FOR_PROCESSING = "ready_for_processing"


class AudioFormat(StrEnum):
    WAV = "wav"
    M4A = "m4a"
    FLAC = "flac"


class AudioReplicaState(StrEnum):
    DISCOVERED = "discovered"
    RECEIVING = "receiving"
    STORED = "stored"
    VERIFIED = "verified"
    AVAILABLE = "available"
    FAILED_RETRYABLE = "failed_retryable"
    QUARANTINED = "quarantined"
    DELETE_PENDING = "delete_pending"
    DELETED = "deleted"


class ProcessingStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_REVIEW = "waiting_review"
    SUCCEEDED = "succeeded"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_FINAL = "failed_final"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    STALE = "stale"


class ChangeOperation(StrEnum):
    UPSERT = "upsert"
    TOMBSTONE = "tombstone"


@dataclass(frozen=True)
class Device:
    device_id: str
    kind: DeviceKind
    name: str
    status: DeviceStatus
    revision: int
    last_seen_at: datetime | None
    created_at: datetime
    updated_at: datetime
    legacy_ref: str | None = None

    def __post_init__(self) -> None:
        _positive_revision(self.revision)


@dataclass(frozen=True)
class RecordingSession:
    session_id: str
    captured_start: datetime
    captured_end: datetime | None
    timezone: str
    state: RecordingSessionState
    revision: int
    status_code: str
    current_stage: str | None
    progress: float
    blocking_reason: str | None
    created_at: datetime
    updated_at: datetime
    legacy_ref: str | None = None

    def __post_init__(self) -> None:
        _positive_revision(self.revision)
        if not 0.0 <= self.progress <= 1.0:
            raise ValueError("progress must be between 0 and 1")
        if self.captured_end is not None and self.captured_end < self.captured_start:
            raise ValueError("captured_end cannot precede captured_start")


@dataclass(frozen=True)
class AudioAsset:
    asset_id: str
    sha256: str
    size_bytes: int
    duration_ms: int
    format: AudioFormat
    media_id: str
    created_at: datetime
    legacy_ref: str | None = None

    def __post_init__(self) -> None:
        if len(self.sha256) != 64:
            raise ValueError("sha256 must contain 64 hexadecimal characters")
        int(self.sha256, 16)
        if self.size_bytes < 0 or self.duration_ms < 0:
            raise ValueError("asset sizes and durations cannot be negative")


@dataclass(frozen=True)
class AudioReplica:
    replica_id: str
    asset_id: str
    device_id: str
    storage_key: str
    state: AudioReplicaState
    verified_at: datetime | None
    created_at: datetime
    legacy_ref: str | None = None


@dataclass(frozen=True)
class CaptureSegment:
    segment_id: str
    session_id: str
    asset_id: str
    replica_id: str
    sequence: int
    session_start_ms: int
    session_end_ms: int
    source_start_ms: int
    source_end_ms: int
    start_sample: int | None
    captured_at: datetime
    legacy_ref: str | None = None

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError("segment sequence cannot be negative")
        if self.session_end_ms <= self.session_start_ms:
            raise ValueError("session segment range must be non-empty")
        if self.source_end_ms <= self.source_start_ms:
            raise ValueError("source segment range must be non-empty")


@dataclass(frozen=True)
class SessionManifest:
    manifest_id: str
    session_id: str
    schema_version: str
    sha256: str
    storage_ref: str
    entries: dict[str, Any]
    created_at: datetime
    legacy_ref: str | None = None


@dataclass(frozen=True)
class ProcessingRun:
    run_id: str
    session_id: str
    pipeline_version: str
    input_revision: int
    status: ProcessingStatus
    config_digest: str
    current_stage: str | None
    progress: float
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    error: str | None = None
    legacy_ref: str | None = None

    def __post_init__(self) -> None:
        _positive_revision(self.input_revision)
        if not 0.0 <= self.progress <= 1.0:
            raise ValueError("progress must be between 0 and 1")


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    kind: str
    producer: str
    producer_version: str
    config_digest: str
    input_refs: tuple[str, ...]
    storage_ref: str
    status: str
    created_at: datetime
    run_id: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    legacy_ref: str | None = None


@dataclass(frozen=True)
class CorrectionOperation:
    correction_id: str
    target_type: str
    target_id: str
    before_revision: int | None
    patch: dict[str, Any]
    actor: str
    created_at: datetime
    legacy_ref: str | None = None


@dataclass(frozen=True)
class ChangeEvent:
    sequence: int
    resource_type: str
    resource_id: str
    revision: int
    operation: ChangeOperation
    payload: dict[str, Any] | None
    created_at: datetime


def _positive_revision(value: int) -> None:
    if value < 1:
        raise ValueError("revision must be positive")


__all__ = [
    "Artifact",
    "AudioAsset",
    "AudioFormat",
    "AudioReplica",
    "AudioReplicaState",
    "CaptureSegment",
    "ChangeEvent",
    "ChangeOperation",
    "CorrectionOperation",
    "Device",
    "DeviceKind",
    "DeviceStatus",
    "ProcessingRun",
    "ProcessingStatus",
    "RecordingSession",
    "RecordingSessionState",
    "SessionManifest",
]
