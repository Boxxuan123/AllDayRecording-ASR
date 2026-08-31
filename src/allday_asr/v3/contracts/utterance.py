from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Protocol, TypedDict, cast

_STABLE_ID_SCHEMA: dict[str, Any] = {
    "type": "string",
    "oneOf": [
        {
            "pattern": (
                "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-"
                "[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
            )
        },
        {"pattern": "^[0-9A-HJKMNP-TV-Z]{26}$"},
    ],
}

# Generated runtime snapshot of contracts/v3/schemas/utterance.schema.json.
# Contract tests exercise the canonical JSON Schema and this validator with the
# same valid/invalid corpus so an edited wire contract cannot silently diverge.
UTTERANCE_DTO_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "utterance_id",
        "session_id",
        "speaker_track_id",
        "speaker_label",
        "start_ms",
        "end_ms",
        "text",
        "revision",
        "status",
        "evidence",
    ],
    "properties": {
        "utterance_id": _STABLE_ID_SCHEMA,
        "session_id": _STABLE_ID_SCHEMA,
        "speaker_track_id": {
            "oneOf": [_STABLE_ID_SCHEMA, {"type": "null"}],
        },
        "speaker_label": {"type": ["string", "null"]},
        "start_ms": {"type": "integer", "minimum": 0},
        "end_ms": {"type": "integer", "minimum": 1},
        "text": {"type": "string"},
        "revision": {"type": "integer", "minimum": 1},
        "status": {"type": "string", "enum": ["active", "stale"]},
        "evidence": {"type": "object"},
    },
}

_STABLE_ID = re.compile(
    r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}|[0-9A-HJKMNP-TV-Z]{26})$"
)
_FIELDS = frozenset(UTTERANCE_DTO_SCHEMA["required"])


class UtteranceDto(TypedDict):
    utterance_id: str
    session_id: str
    speaker_track_id: str | None
    speaker_label: str | None
    start_ms: int
    end_ms: int
    text: str
    revision: int
    status: str
    evidence: dict[str, Any]


class UtteranceSource(Protocol):
    utterance_id: str
    session_id: str
    speaker_track_id: str | None
    start_ms: int
    end_ms: int
    text: str
    revision: int
    status: str
    evidence: dict[str, Any]


def utterance_dto(
    value: UtteranceSource, *, speaker_label: str | None
) -> UtteranceDto:
    return validate_utterance_dto(
        {
            "utterance_id": value.utterance_id,
            "session_id": value.session_id,
            "speaker_track_id": value.speaker_track_id,
            "speaker_label": speaker_label,
            "start_ms": value.start_ms,
            "end_ms": value.end_ms,
            "text": value.text,
            "revision": value.revision,
            "status": value.status,
            "evidence": value.evidence,
        }
    )


def validate_utterance_dto(payload: Mapping[str, Any]) -> UtteranceDto:
    value = dict(payload)
    if set(value) != _FIELDS:
        raise ValueError("utterance DTO fields do not match schema")
    if not _stable_id(value["utterance_id"]):
        raise ValueError("utterance DTO utterance_id violates schema")
    if not _stable_id(value["session_id"]):
        raise ValueError("utterance DTO session_id violates schema")
    speaker_track_id = value["speaker_track_id"]
    if speaker_track_id is not None and not _stable_id(speaker_track_id):
        raise ValueError("utterance DTO speaker_track_id violates schema")
    speaker_label = value["speaker_label"]
    if speaker_label is not None and not isinstance(speaker_label, str):
        raise ValueError("utterance DTO speaker_label violates schema")
    start_ms = value["start_ms"]
    end_ms = value["end_ms"]
    if not _integer(start_ms) or start_ms < 0:
        raise ValueError("utterance DTO start_ms violates schema")
    if not _integer(end_ms) or end_ms <= start_ms:
        raise ValueError("utterance DTO end_ms violates schema")
    if not isinstance(value["text"], str):
        raise ValueError("utterance DTO text violates schema")
    revision = value["revision"]
    if not _integer(revision) or revision < 1:
        raise ValueError("utterance DTO revision violates schema")
    if value["status"] not in {"active", "stale"}:
        raise ValueError("utterance DTO status violates schema")
    if not isinstance(value["evidence"], Mapping):
        raise ValueError("utterance DTO evidence violates schema")
    value["evidence"] = dict(value["evidence"])
    return cast(UtteranceDto, value)


def _stable_id(value: object) -> bool:
    return isinstance(value, str) and _STABLE_ID.fullmatch(value) is not None


def _integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


__all__ = [
    "UTTERANCE_DTO_SCHEMA",
    "UtteranceDto",
    "utterance_dto",
    "validate_utterance_dto",
]
