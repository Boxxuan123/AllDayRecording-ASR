from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol

from allday_asr.v3.domain.device_sync import (
    PROJECTION_VERSION,
    ClientOperation,
    ClientOperationStatus,
    OperationReceipt,
    SyncChange,
    SyncRequest,
    SyncResponse,
)
from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.ports.repositories import UnitOfWork


UnitOfWorkFactory = Callable[[], UnitOfWork]
DateTimeClock = Callable[[], datetime]
_CURSOR = re.compile(r"^cursor-(0|[1-9][0-9]*)$")


class ClientOperationHandler(Protocol):
    def apply(
        self, operation: ClientOperation, uow: UnitOfWork
    ) -> OperationReceipt: ...


class RejectingClientOperationHandler:
    def apply(
        self, operation: ClientOperation, uow: UnitOfWork
    ) -> OperationReceipt:
        del uow
        return OperationReceipt(
            operation_id=operation.operation_id,
            status=ClientOperationStatus.REJECTED,
            resource_revision=None,
            error=_error(
                "UNSUPPORTED_OPERATION",
                f"unsupported client operation: {operation.kind}",
            ),
        )


class MobileSyncService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        operation_handler: ClientOperationHandler | None = None,
        now: DateTimeClock | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._handler = operation_handler or RejectingClientOperationHandler()
        self._now = now or (lambda: datetime.now(timezone.utc))

    def synchronize(self, device_id: str, request: SyncRequest) -> SyncResponse:
        cursor_sequence = _decode_cursor(request.cursor)
        receipts: list[OperationReceipt] = []
        with self._uow_factory() as uow:
            for operation in request.client_operations:
                receipts.append(self._apply_once(device_id, operation, uow))
            events = uow.changes.list_after(
                cursor_sequence,
                request.pull_limit + 1,
            )
            selected = events[: request.pull_limit]
            changes = tuple(
                SyncChange(
                    sequence=event.sequence,
                    resource_type=event.resource_type,
                    resource_id=event.resource_id,
                    revision=event.revision,
                    operation=event.operation.value,
                    resource=event.payload,
                )
                for event in selected
            )
            next_sequence = (
                selected[-1].sequence if selected else cursor_sequence
            )
            uow.mobile_sync.acknowledge_cursor(
                device_id,
                PROJECTION_VERSION,
                cursor_sequence,
            )
            uow.audit.append(
                "device.sync",
                f"device:{device_id}",
                "device",
                device_id,
                {
                    "cursor": request.cursor,
                    "operation_count": len(request.client_operations),
                    "change_count": len(changes),
                    "next_cursor": _encode_cursor(next_sequence),
                },
            )
        return SyncResponse(
            projection_version=PROJECTION_VERSION,
            receipts=tuple(receipts),
            changes=changes,
            next_cursor=_encode_cursor(next_sequence),
            has_more=len(events) > request.pull_limit,
            server_time=self._now().astimezone(timezone.utc),
        )

    def _apply_once(
        self,
        device_id: str,
        operation: ClientOperation,
        uow: UnitOfWork,
    ) -> OperationReceipt:
        digest = _operation_sha256(operation)
        existing = uow.mobile_sync.find_operation(operation.operation_id)
        if existing is not None:
            if existing.device_id == device_id and existing.payload_sha256 == digest:
                return existing.receipt
            return OperationReceipt(
                operation_id=operation.operation_id,
                status=ClientOperationStatus.CONFLICT,
                resource_revision=None,
                error=_error(
                    "OPERATION_ID_REUSED",
                    "operation_id was already used with different content",
                ),
            )
        receipt = self._handler.apply(operation, uow)
        if (
            receipt.operation_id != operation.operation_id
            or receipt.status is ClientOperationStatus.PENDING
        ):
            raise ValueError("client operation handler returned an invalid receipt")
        if not uow.mobile_sync.record_operation(
            device_id,
            operation,
            digest,
            receipt,
        ):
            raise RuntimeError("client operation raced with another transaction")
        return receipt


def _operation_sha256(operation: ClientOperation) -> str:
    payload = json.dumps(
        {
            "operation_id": operation.operation_id,
            "kind": operation.kind,
            "base_revision": operation.base_revision,
            "payload": operation.payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _decode_cursor(value: str | None) -> int:
    if value is None:
        return 0
    match = _CURSOR.fullmatch(value)
    if match is None:
        raise ValueError("invalid sync cursor")
    return int(match.group(1))


def _encode_cursor(sequence: int) -> str:
    return f"cursor-{sequence}"


def _error(code: str, message: str) -> dict[str, object]:
    return {
        "code": code,
        "message": message,
        "details": {},
        "request_id": new_ulid(),
    }


__all__ = [
    "ClientOperationHandler",
    "MobileSyncService",
    "RejectingClientOperationHandler",
]
