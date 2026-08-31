from __future__ import annotations

from collections.abc import Mapping
from typing import Any


UNKNOWN_ENUM_VALUE = "unknown"

# Generated snapshots of contracts/v3/schemas/primitives.schema.json. Contract
# tests fail when these diverge from the canonical schema.
KNOWN_ENUMS: dict[str, frozenset[str]] = {
    "recording_session_state": frozenset(
        {
            "capturing",
            "closing",
            "recovering",
            "quarantined",
            "sealed",
            "phone_verified",
            "computer_ingested",
            "admission_pending",
            "admission_blocked",
            "ready_for_processing",
        }
    ),
    "aggregate_status": frozenset(
        {
            "awaiting_upload",
            "verifying",
            "backup_required",
            "ready",
            "processing",
            "needs_review",
            "available",
            "failed",
            "stale",
        }
    ),
    "processing_status": frozenset(
        {
            "created",
            "queued",
            "running",
            "waiting_review",
            "succeeded",
            "failed_retryable",
            "failed_final",
            "cancel_requested",
            "cancelled",
            "stale",
        }
    ),
    "receipt_status": frozenset(
        {"pending", "applied", "conflict", "rejected"}
    ),
    "resource_type": frozenset(
        {
            "recording_session",
            "audio_asset",
            "processing_run",
            "utterance",
            "review_item",
        }
    ),
    "sync_operation": frozenset({"upsert", "tombstone"}),
}


def normalize_enum(value: object, enum_name: str) -> str:
    known = KNOWN_ENUMS[enum_name]
    return value if isinstance(value, str) and value in known else UNKNOWN_ENUM_VALUE


def decode_contract_fixture(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Decode the shared fixture into the cross-client comparison projection."""
    session = _mapping(payload, "recording_session")
    processing_run = _mapping(payload, "processing_run")
    sync_response = _mapping(payload, "device_sync_response")
    receipts = _sequence(sync_response, "receipts")
    changes = _sequence(sync_response, "changes")
    receipt = _first_mapping(receipts)
    change = _first_mapping(changes)
    next_cursor = sync_response.get("next_cursor")
    return {
        "contract_version": _string(payload, "contract_version"),
        "session_state": normalize_enum(
            session.get("state"), "recording_session_state"
        ),
        "aggregate_status": normalize_enum(
            session.get("status_code"), "aggregate_status"
        ),
        "processing_status": normalize_enum(
            processing_run.get("status"), "processing_status"
        ),
        "receipt_status": normalize_enum(
            receipt.get("status"), "receipt_status"
        ),
        "change_resource_type": normalize_enum(
            change.get("resource_type"), "resource_type"
        ),
        "change_operation": normalize_enum(
            change.get("operation"), "sync_operation"
        ),
        "next_cursor": next_cursor if isinstance(next_cursor, str) else None,
    }


def _mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} must be an object")
    return value


def _sequence(payload: Mapping[str, Any], key: str) -> list[Any]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be an array")
    return value


def _first_mapping(values: list[Any]) -> Mapping[str, Any]:
    if not values or not isinstance(values[0], Mapping):
        raise ValueError("fixture enum collection must contain an object")
    return values[0]


def _string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


__all__ = [
    "KNOWN_ENUMS",
    "UNKNOWN_ENUM_VALUE",
    "decode_contract_fixture",
    "normalize_enum",
]
