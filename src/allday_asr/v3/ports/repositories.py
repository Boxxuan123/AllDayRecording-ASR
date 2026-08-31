from __future__ import annotations

from types import TracebackType
from typing import Any, Protocol, Self

from allday_asr.v3.domain.models import (
    Artifact,
    AudioAsset,
    AudioReplica,
    CaptureSegment,
    CorrectionOperation,
    Device,
    ProcessingRun,
    RecordingSession,
    SessionManifest,
)


class RecordingCatalogRepository(Protocol):
    def add_session(self, session: RecordingSession) -> bool: ...
    def add_asset(self, asset: AudioAsset) -> bool: ...
    def add_replica(self, replica: AudioReplica) -> bool: ...
    def add_segment(self, segment: CaptureSegment) -> bool: ...
    def add_manifest(self, manifest: SessionManifest) -> bool: ...
    def find_session_by_legacy_ref(self, legacy_ref: str) -> RecordingSession | None: ...
    def find_asset_by_sha256(self, sha256: str) -> AudioAsset | None: ...


class DeviceRepository(Protocol):
    def add(self, device: Device) -> bool: ...


class ProcessingRunRepository(Protocol):
    def add(self, run: ProcessingRun) -> bool: ...
    def find_by_legacy_ref(self, legacy_ref: str) -> ProcessingRun | None: ...


class ArtifactRepository(Protocol):
    def add(self, artifact: Artifact) -> bool: ...


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
    artifacts: ArtifactRepository
    corrections: CorrectionRepository
    changes: ChangeLogRepository
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
    "AuditRepository",
    "ChangeLogRepository",
    "CorrectionRepository",
    "DeviceRepository",
    "IdempotencyRepository",
    "LegacyImportRunRepository",
    "ProcessingRunRepository",
    "RecordingCatalogRepository",
    "TombstoneRepository",
    "UnitOfWork",
]
