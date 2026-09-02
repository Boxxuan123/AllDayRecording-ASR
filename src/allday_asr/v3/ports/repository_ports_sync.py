from __future__ import annotations
from typing import Any, Protocol
from allday_asr.v3.domain.models import (
    ChangeEvent,
)
from allday_asr.v3.domain.device_sync import (
    ClientOperation,
    ClientOperationRecord,
    DeviceCredential,
    DeviceScope,
    OperationReceipt,
    PairingRecord,
)


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

    def latest(self, resource_type: str, resource_id: str) -> ChangeEvent | None: ...


class DeviceTrustRepository(Protocol):
    def enroll(self, credential: DeviceCredential, pairing: PairingRecord) -> bool: ...

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


class DesktopReadRepository(Protocol):
    def overview(self) -> dict[str, Any]: ...
    def list_sessions(
        self,
        before_captured_start: str | None,
        before_session_id: str | None,
        limit: int,
        search: str | None = None,
    ) -> tuple[dict[str, Any], ...]: ...
    def session_detail(self, session_id: str) -> dict[str, Any]: ...
    def session_audio_segments(
        self, session_id: str, start_ms: int, end_ms: int
    ) -> tuple[dict[str, Any], ...]: ...
    def list_processing_jobs(
        self, status: str | None, limit: int
    ) -> tuple[dict[str, Any], ...]: ...
    def list_reviews(self, limit: int) -> tuple[dict[str, Any], ...]: ...
    def list_devices(self) -> tuple[dict[str, Any], ...]: ...
    def data_health(self) -> dict[str, Any]: ...
    def media(self, media_id: str) -> dict[str, Any]: ...
    def processing_events(
        self, after_sequence: int, limit: int
    ) -> tuple[dict[str, Any], ...]: ...
