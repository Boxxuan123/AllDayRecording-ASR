from __future__ import annotations

from types import TracebackType
from typing import Any, Protocol, Self

from allday_asr.v3.domain.models import (
    Artifact,
    AudioAsset,
    AudioReplica,
    CaptureSegment,
    ChangeEvent,
    CorrectionOperation,
    Device,
    ProcessingRun,
    RecordingSession,
    SessionManifest,
)
from allday_asr.v3.domain.device_sync import (
    ClientOperation,
    ClientOperationRecord,
    DeviceCredential,
    DeviceScope,
    OperationReceipt,
    PairingRecord,
)
from allday_asr.v3.domain.processing import (
    ArtifactInvalidation,
    BackupEvidence,
    ProcessingClaim,
    ProcessingJob,
    ProcessingSnapshot,
    SpeakerTrack,
    StageRun,
    Utterance,
)


class RecordingCatalogRepository(Protocol):
    def add_session(self, session: RecordingSession) -> bool: ...
    def add_asset(self, asset: AudioAsset) -> bool: ...
    def add_replica(self, replica: AudioReplica) -> bool: ...
    def add_segment(self, segment: CaptureSegment) -> bool: ...
    def add_manifest(self, manifest: SessionManifest) -> bool: ...
    def find_session_by_legacy_ref(self, legacy_ref: str) -> RecordingSession | None: ...
    def find_asset_by_sha256(self, sha256: str) -> AudioAsset | None: ...
    def get_session(self, session_id: str) -> RecordingSession: ...


class DeviceRepository(Protocol):
    def add(self, device: Device) -> bool: ...


class ProcessingRunRepository(Protocol):
    def add(self, run: ProcessingRun) -> bool: ...
    def find_by_legacy_ref(self, legacy_ref: str) -> ProcessingRun | None: ...
    def get(self, run_id: str) -> ProcessingRun: ...


class ArtifactRepository(Protocol):
    def add(self, artifact: Artifact) -> bool: ...
    def list_active_for_run(self, run_id: str) -> tuple[Artifact, ...]: ...
    def add_dependency(
        self, artifact_id: str, input_type: str, input_id: str, input_revision: int
    ) -> bool: ...
    def dependent_ids(self, input_type: str, input_id: str) -> tuple[str, ...]: ...
    def invalidate(self, event: ArtifactInvalidation) -> bool: ...


class DurableProcessingRepository(Protocol):
    def find_effective_run(
        self, session_id: str, input_revision: int, pipeline_version: str
    ) -> ProcessingRun | None: ...
    def add_graph(
        self,
        run: ProcessingRun,
        job: ProcessingJob,
        stages: tuple[StageRun, ...],
    ) -> None: ...
    def get_snapshot(self, job_id: str) -> ProcessingSnapshot: ...
    def get_snapshot_for_run(self, run_id: str) -> ProcessingSnapshot: ...
    def claim_next(
        self, worker_id: str, lease_seconds: int, config: dict[str, Any]
    ) -> ProcessingClaim | None: ...
    def heartbeat(
        self,
        claim: ProcessingClaim,
        lease_seconds: int,
        checkpoint: dict[str, Any] | None,
    ) -> bool: ...
    def complete_stage(
        self,
        claim: ProcessingClaim,
        checkpoint: dict[str, Any],
        log_summary: str,
        output: dict[str, Any],
    ) -> ProcessingSnapshot: ...
    def fail_stage(
        self,
        claim: ProcessingClaim,
        error: str,
        log_summary: str,
        retryable: bool,
    ) -> ProcessingSnapshot: ...
    def cancel_claim(self, claim: ProcessingClaim, reason: str) -> ProcessingSnapshot: ...
    def request_cancel(self, job_id: str, reason: str) -> ProcessingSnapshot: ...
    def retry(self, job_id: str) -> ProcessingSnapshot: ...
    def recover_expired(self) -> tuple[str, ...]: ...


class AdmissionRepository(Protocol):
    def add_evidence(self, evidence: BackupEvidence) -> bool: ...
    def evaluate(self, session_id: str) -> tuple[bool, str | None]: ...
    def apply(self, session_id: str, admitted: bool, reason: str | None) -> int: ...


class EvidenceProjectionRepository(Protocol):
    def add_speaker_track(self, track: SpeakerTrack) -> bool: ...
    def add_utterance(self, utterance: Utterance) -> bool: ...
    def get_utterance(self, utterance_id: str) -> Utterance: ...
    def revise_utterance(
        self, utterance_id: str, expected_revision: int, text: str
    ) -> Utterance: ...


class CorrectionRepository(Protocol):
    def add(self, correction: CorrectionOperation) -> bool: ...


class ChangeLogRepository(Protocol):
    def append(
        self,
        resource_type: str,
        resource_id: str,
        revision: int,
        operation: str,
        payload: dict[str, Any] | None,
    ) -> int: ...

    def list_after(self, sequence: int, limit: int) -> tuple[ChangeEvent, ...]: ...


class DeviceTrustRepository(Protocol):
    def enroll(
        self, credential: DeviceCredential, pairing: PairingRecord
    ) -> bool: ...

    def find_by_key_id(self, key_id: str) -> DeviceCredential | None: ...

    def authorize(
        self, key_id: str, required_scopes: tuple[DeviceScope, ...]
    ) -> DeviceCredential: ...

    def mark_used(self, key_id: str) -> None: ...

    def revoke(self, key_id: str, revoked_at: str) -> bool: ...


class MobileSyncRepository(Protocol):
    def find_operation(self, operation_id: str) -> ClientOperationRecord | None: ...

    def record_operation(
        self,
        device_id: str,
        operation: ClientOperation,
        payload_sha256: str,
        receipt: OperationReceipt,
    ) -> bool: ...

    def acknowledge_cursor(
        self, device_id: str, projection_version: int, sequence: int
    ) -> None: ...


class AuditRepository(Protocol):
    def append(
        self,
        action: str,
        actor: str,
        target_type: str,
        target_id: str,
        details: dict[str, Any],
        *,
        legacy_ref: str | None = None,
    ) -> bool: ...


class IdempotencyRepository(Protocol):
    def begin(self, key: str, command: str) -> bool: ...
    def complete(self, key: str, response: dict[str, Any]) -> None: ...
    def response(self, key: str) -> dict[str, Any] | None: ...


class TombstoneRepository(Protocol):
    def add(
        self,
        resource_type: str,
        resource_id: str,
        revision: int,
        reason: str | None,
    ) -> bool: ...


class LegacyImportRunRepository(Protocol):
    def start(
        self,
        import_id: str,
        source_namespace: str,
        source_path: str,
        source_database_sha256: str,
        source_schema_version: int,
    ) -> None: ...

    def complete(self, import_id: str, report: dict[str, Any]) -> None: ...

    def fail(self, import_id: str, report: dict[str, Any]) -> None: ...


class UnitOfWork(Protocol):
    catalog: RecordingCatalogRepository
    devices: DeviceRepository
    processing_runs: ProcessingRunRepository
    processing: DurableProcessingRepository
    admission: AdmissionRepository
    artifacts: ArtifactRepository
    evidence: EvidenceProjectionRepository
    corrections: CorrectionRepository
    changes: ChangeLogRepository
    device_trust: DeviceTrustRepository
    mobile_sync: MobileSyncRepository
    audit: AuditRepository
    idempotency: IdempotencyRepository
    tombstones: TombstoneRepository
    legacy_imports: LegacyImportRunRepository

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


__all__ = [
    "ArtifactRepository",
    "AdmissionRepository",
    "AuditRepository",
    "ChangeLogRepository",
    "CorrectionRepository",
    "DeviceRepository",
    "DeviceTrustRepository",
    "DurableProcessingRepository",
    "EvidenceProjectionRepository",
    "IdempotencyRepository",
    "LegacyImportRunRepository",
    "MobileSyncRepository",
    "ProcessingRunRepository",
    "RecordingCatalogRepository",
    "TombstoneRepository",
    "UnitOfWork",
]
