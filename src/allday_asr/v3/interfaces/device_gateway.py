from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from allday_asr.v3.interfaces.transfer.devices import (
    DeviceCredentialRecord as LegacyDeviceCredentialRecord,
)
from allday_asr.v3.interfaces.transfer.store import UploadRecord, UploadStore
from allday_asr.v3 import CONTRACT_VERSION, PROJECTION_VERSION
from allday_asr.v3.adapters.transfer import (
    TransferDeviceTrustAdapter,
    V3UploadIngestAdapter,
)
from allday_asr.v3.application import MobileSyncService
from allday_asr.v3.domain.device_sync import ClientOperation, SyncRequest


_STABLE_ID = re.compile(
    r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-"
    r"[0-9a-f]{12}|[0-9A-HJKMNP-TV-Z]{26})$"
)


class DeviceGateway:
    def __init__(
        self,
        trust: TransferDeviceTrustAdapter,
        sync: MobileSyncService,
        ingest: V3UploadIngestAdapter | None = None,
        session_ingested: Callable[
            [UploadRecord, Mapping[str, Any]], Mapping[str, Any] | None
        ]
        | None = None,
    ) -> None:
        self.trust = trust
        self.sync_service = sync
        self.ingest = ingest
        self.session_ingested = session_ingested

    def reconcile(self) -> int:
        return self.trust.reconcile()

    def device_enrolled(self, record: LegacyDeviceCredentialRecord) -> None:
        self.trust.enroll(record)

    def status(self, key_id: str) -> dict[str, Any]:
        self.trust.domain_device_id(key_id)
        return {
            "contract_version": CONTRACT_VERSION,
            "projection_version": PROJECTION_VERSION,
            "server_time": _utc_now(),
        }

    def synchronize(
        self, key_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        request = parse_sync_request(payload)
        device_id = self.trust.domain_device_id(key_id)
        return self.sync_service.synchronize(device_id, request).as_dict()

    def upload_completed(
        self,
        key_id: str,
        record: UploadRecord,
        store: UploadStore,
    ) -> dict[str, Any] | None:
        if self.ingest is None:
            return None
        result = self.ingest.ingest_completed(key_id, record, store)
        if result is None:
            return None
        response = dict(result)
        if self.session_ingested is not None:
            automation = self.session_ingested(record, response)
            if automation is not None:
                response["automation"] = dict(automation)
        return response


def parse_sync_request(payload: Mapping[str, Any]) -> SyncRequest:
    required = {
        "projection_version",
        "cursor",
        "client_operations",
        "pull_limit",
    }
    if set(payload) != required:
        raise ValueError("sync request fields do not match the V3 contract")
    raw_operations = payload["client_operations"]
    if not isinstance(raw_operations, list):
        raise ValueError("client_operations must be an array")
    operations: list[ClientOperation] = []
    for raw in raw_operations:
        if not isinstance(raw, dict) or set(raw) != {
            "operation_id",
            "kind",
            "base_revision",
            "payload",
        }:
            raise ValueError("client operation fields are invalid")
        operation_id = raw["operation_id"]
        kind = raw["kind"]
        base_revision = raw["base_revision"]
        operation_payload = raw["payload"]
        if (
            not isinstance(operation_id, str)
            or _STABLE_ID.fullmatch(operation_id) is None
            or not isinstance(kind, str)
            or not kind
            or len(kind) > 120
            or (
                base_revision is not None
                and (isinstance(base_revision, bool) or not isinstance(base_revision, int))
            )
            or not isinstance(operation_payload, dict)
        ):
            raise ValueError("client operation values are invalid")
        operations.append(
            ClientOperation(
                operation_id=operation_id,
                kind=kind,
                base_revision=base_revision,
                payload=dict(operation_payload),
            )
        )
    projection_version = payload["projection_version"]
    pull_limit = payload["pull_limit"]
    cursor = payload["cursor"]
    if isinstance(projection_version, bool) or not isinstance(
        projection_version, int
    ):
        raise ValueError("projection_version must be an integer")
    if isinstance(pull_limit, bool) or not isinstance(pull_limit, int):
        raise ValueError("pull_limit must be an integer")
    if cursor is not None and not isinstance(cursor, str):
        raise ValueError("cursor must be a string or null")
    return SyncRequest(
        projection_version=projection_version,
        cursor=cursor,
        client_operations=tuple(operations),
        pull_limit=pull_limit,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = ["DeviceGateway", "parse_sync_request"]
