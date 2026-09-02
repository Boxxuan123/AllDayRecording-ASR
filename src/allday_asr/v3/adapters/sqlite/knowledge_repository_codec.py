from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from allday_asr.v3.domain.knowledge import (
    EventCurrentState,
    EventKind,
    EventStatus,
    GenerationRecord,
    GenerationStatus,
    KnowledgeLayer,
    ProposalKind,
    ProposalStatus,
    StructuredProposal,
)


def _generation(row: sqlite3.Row) -> GenerationRecord:
    return GenerationRecord(
        generation_id=str(row["generation_id"]),
        layer=KnowledgeLayer(str(row["layer"])),
        producer=str(row["producer"]),
        producer_version=str(row["producer_version"]),
        model=str(row["model"]),
        prompt_version=str(row["prompt_version"]),
        extractor_version=str(row["extractor_version"]),
        input_scope=_object(row["input_scope_json"]),
        input_sha256=str(row["input_sha256"]),
        generation_number=int(row["generation_number"]),
        status=GenerationStatus(str(row["status"])),
        error=str(row["error"]) if row["error"] is not None else None,
        created_at=_parse_datetime(row["created_at"]),
        completed_at=_optional_parse_datetime(row["completed_at"]),
    )


def _proposal(row: sqlite3.Row) -> StructuredProposal:
    evidence = json.loads(str(row["evidence_utterance_ids_json"]))
    if not isinstance(evidence, list) or not all(
        isinstance(item, str) for item in evidence
    ):
        raise ValueError("stored proposal evidence is invalid")
    return StructuredProposal(
        proposal_id=str(row["proposal_id"]),
        generation_id=str(row["generation_id"]),
        kind=ProposalKind(str(row["kind"])),
        payload=_object(row["payload_json"]),
        evidence_utterance_ids=tuple(evidence),
        status=ProposalStatus(str(row["status"])),
        created_at=_parse_datetime(row["created_at"]),
        resolved_at=_optional_parse_datetime(row["resolved_at"]),
        resolved_by=str(row["resolved_by"]) if row["resolved_by"] else None,
        resolution_reason=(
            str(row["resolution_reason"]) if row["resolution_reason"] else None
        ),
    )


def _proposal_dict(row: sqlite3.Row) -> dict[str, Any]:
    value = _dict(row)
    value["payload"] = _object(value.pop("payload_json"))
    value["evidence_utterance_ids"] = json.loads(
        str(value.pop("evidence_utterance_ids_json"))
    )
    value["input_scope"] = _object(value.pop("input_scope_json"))
    return value


def _event_state(row: sqlite3.Row) -> EventCurrentState:
    return EventCurrentState(
        event_id=str(row["event_id"]),
        session_id=str(row["session_id"]),
        event_kind=EventKind(str(row["event_kind"])),
        status=EventStatus(str(row["status"])),
        revision=int(row["revision"]),
        payload=_object(row["payload_json"]),
        latest_operation_id=str(row["latest_operation_id"]),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
        derivation_status=str(row["derivation_status"]),
    )


def _dict(row: sqlite3.Row) -> dict[str, Any]:
    return {str(key): row[key] for key in row.keys()}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _object(value: object) -> dict[str, Any]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise ValueError("stored JSON value is not an object")
    return parsed


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
