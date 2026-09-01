from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from allday_asr.v3.domain.identity import SelfIdentity

from allday_asr.v3.domain.models import ProcessingRun


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_REVIEW = "waiting_review"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STALE = "stale"


class AttemptStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LOST_LEASE = "lost_lease"


class LeaseStatus(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"
    LOST = "lost"


class BackupEvidenceStatus(StrEnum):
    VERIFIED = "verified"
    FAILED = "failed"


@dataclass(frozen=True)
class StageDefinition:
    name: str
    optional: bool = False


DEFAULT_PROCESSING_STAGES = (
    StageDefinition("ingest_verified"),
    StageDefinition("backup_admitted"),
    StageDefinition("window_plan"),
    StageDefinition("speech_gate"),
    StageDefinition("asr_and_alignment"),
    StageDefinition("diarization"),
    StageDefinition("utterance_projection"),
    StageDefinition("semantic_evidence_optional", optional=True),
    StageDefinition("mobile_projection"),
)


@dataclass(frozen=True)
class ProcessingJob:
    job_id: str
    run_id: str
    kind: str
    status: str
    priority: int
    request: dict[str, Any]
    available_at: datetime
    created_at: datetime
    updated_at: datetime
    lease_owner: str | None = None
    heartbeat_at: datetime | None = None
    completed_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


@dataclass(frozen=True)
class StageRun:
    stage_run_id: str
    run_id: str
    stage: str
    ordinal: int
    optional: bool
    status: StageStatus
    progress: float
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    output: dict[str, Any] | None = None
    error: str | None = None


@dataclass(frozen=True)
class StageAttempt:
    attempt_id: str
    stage_run_id: str
    attempt_number: int
    status: AttemptStatus
    worker_id: str
    config: dict[str, Any]
    started_at: datetime
    heartbeat_at: datetime
    checkpoint: dict[str, Any] | None = None
    log_summary: str | None = None
    error: str | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True)
class WorkerLease:
    lease_id: str
    job_id: str
    attempt_id: str
    worker_id: str
    lease_token: str
    status: LeaseStatus
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime
    released_at: datetime | None = None


@dataclass(frozen=True)
class ProcessingClaim:
    job: ProcessingJob
    run: ProcessingRun
    stage: StageRun
    attempt: StageAttempt
    lease: WorkerLease
    resume_checkpoint: dict[str, Any] | None = None


@dataclass(frozen=True)
class BackupEvidence:
    evidence_id: str
    session_id: str
    provider: str
    storage_kind: str
    digest: str
    status: BackupEvidenceStatus
    metadata: dict[str, Any]
    created_at: datetime
    restore_checked_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.storage_kind not in {"independent_device", "network"}:
            raise ValueError("backup storage kind is not independent")
        if len(self.digest) != 64:
            raise ValueError("backup digest must be SHA-256")
        int(self.digest, 16)
        if self.status is BackupEvidenceStatus.VERIFIED and self.restore_checked_at is None:
            raise ValueError("verified backup evidence requires a restore check")


@dataclass(frozen=True)
class SpeakerTrack:
    speaker_track_id: str
    session_id: str
    run_id: str
    label: str
    source_artifact_id: str
    created_at: datetime


@dataclass(frozen=True)
class Utterance:
    utterance_id: str
    session_id: str
    run_id: str
    source_artifact_id: str
    speaker_track_id: str | None
    original_speaker_track_id: str | None
    ordinal: int
    start_ms: int
    end_ms: int
    start_at: datetime
    end_at: datetime
    text: str
    original_text: str
    identity: SelfIdentity
    original_identity: SelfIdentity
    identity_evidence: dict[str, Any]
    evidence: dict[str, Any]
    revision: int
    status: str
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.ordinal < 0 or self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("utterance coordinates are invalid")
        if self.end_at <= self.start_at:
            raise ValueError("utterance absolute coordinates are invalid")
        if self.revision < 1:
            raise ValueError("utterance revision must be positive")
        if self.identity_evidence.get("decision") != self.original_identity.value:
            raise ValueError("utterance identity evidence must match original identity")


@dataclass(frozen=True)
class ArtifactInvalidation:
    status_event_id: str
    artifact_id: str
    status: str
    reason: str
    source_type: str
    source_id: str
    source_revision: int
    created_at: datetime


@dataclass(frozen=True)
class ProcessingSnapshot:
    job: ProcessingJob
    run: ProcessingRun
    stages: tuple[StageRun, ...] = field(default_factory=tuple)
    attempts: tuple[StageAttempt, ...] = field(default_factory=tuple)


__all__ = [
    "ArtifactInvalidation",
    "AttemptStatus",
    "BackupEvidence",
    "BackupEvidenceStatus",
    "DEFAULT_PROCESSING_STAGES",
    "LeaseStatus",
    "ProcessingClaim",
    "ProcessingJob",
    "ProcessingSnapshot",
    "SpeakerTrack",
    "StageAttempt",
    "StageDefinition",
    "StageRun",
    "StageStatus",
    "Utterance",
    "WorkerLease",
]
