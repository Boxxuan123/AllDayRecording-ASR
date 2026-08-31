from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


PROJECTION_VERSION = 1
MAX_SYNC_OPERATIONS = 500
MAX_SYNC_CHANGES = 500


class DeviceScope(StrEnum):
    AUDIO_UPLOAD = "audio.upload"
    DATA_SYNC_READ = "data.sync.read"
    DATA_SYNC_WRITE = "data.sync.write"
    DEVICE_STATUS = "device.status"


DEFAULT_PHONE_SCOPES = tuple(DeviceScope)


class ClientOperationStatus(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    CONFLICT = "conflict"
    REJECTED = "rejected"


@dataclass(frozen=True)
class DeviceCredential:
    credential_id: str
    device_id: str
    key_id: str
    algorithm: str
    public_key: str
    scopes: tuple[DeviceScope, ...]
    passkey_credential_ref: str
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None

    def allows(self, required: tuple[DeviceScope, ...]) -> bool:
        return self.revoked_at is None and set(required).issubset(self.scopes)


@dataclass(frozen=True)
class PairingRecord:
    pairing_id: str
    device_id: str
    receiver_id: str
    passkey_credential_ref: str
    paired_at: datetime
    revoked_at: datetime | None = None


@dataclass(frozen=True)
class ClientOperation:
    operation_id: str
    kind: str
    base_revision: int | None
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        if not self.operation_id or not self.kind:
            raise ValueError("operation_id and kind are required")
        if self.base_revision is not None and self.base_revision < 1:
            raise ValueError("base_revision must be positive")


@dataclass(frozen=True)
class OperationReceipt:
    operation_id: str
    status: ClientOperationStatus
    resource_revision: int | None
    error: dict[str, Any] | None

    def __post_init__(self) -> None:
        if not self.operation_id:
            raise ValueError("operation receipt requires operation_id")
        if self.status is ClientOperationStatus.APPLIED:
            if self.resource_revision is None or self.error is not None:
                raise ValueError("applied receipt requires revision and no error")
        elif self.status in {
            ClientOperationStatus.CONFLICT,
            ClientOperationStatus.REJECTED,
        }:
            if self.resource_revision is not None or self.error is None:
                raise ValueError(
                    "conflict/rejected receipt requires error and no revision"
                )

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "status": self.status.value,
            "resource_revision": self.resource_revision,
            "error": self.error,
        }


@dataclass(frozen=True)
class ClientOperationRecord:
    device_id: str
    operation: ClientOperation
    payload_sha256: str
    receipt: OperationReceipt
    created_at: datetime
    completed_at: datetime


@dataclass(frozen=True)
class SyncRequest:
    projection_version: int
    cursor: str | None
    client_operations: tuple[ClientOperation, ...]
    pull_limit: int

    def __post_init__(self) -> None:
        if self.projection_version != PROJECTION_VERSION:
            raise ValueError("unsupported projection version")
        if len(self.client_operations) > MAX_SYNC_OPERATIONS:
            raise ValueError("too many client operations")
        if not 1 <= self.pull_limit <= MAX_SYNC_CHANGES:
            raise ValueError("pull_limit is outside the supported range")


@dataclass(frozen=True)
class SyncChange:
    sequence: int
    resource_type: str
    resource_id: str
    revision: int
    operation: str
    resource: dict[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "revision": self.revision,
            "operation": self.operation,
            "resource": self.resource,
        }


@dataclass(frozen=True)
class SyncResponse:
    projection_version: int
    receipts: tuple[OperationReceipt, ...]
    changes: tuple[SyncChange, ...]
    next_cursor: str
    has_more: bool
    server_time: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "projection_version": self.projection_version,
            "receipts": [receipt.as_dict() for receipt in self.receipts],
            "changes": [change.as_dict() for change in self.changes],
            "next_cursor": self.next_cursor,
            "has_more": self.has_more,
            "server_time": self.server_time.isoformat().replace("+00:00", "Z"),
        }


__all__ = [
    "DEFAULT_PHONE_SCOPES",
    "ClientOperation",
    "ClientOperationRecord",
    "ClientOperationStatus",
    "DeviceCredential",
    "DeviceScope",
    "MAX_SYNC_CHANGES",
    "MAX_SYNC_OPERATIONS",
    "OperationReceipt",
    "PROJECTION_VERSION",
    "PairingRecord",
    "SyncChange",
    "SyncRequest",
    "SyncResponse",
]
