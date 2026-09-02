from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Any


def _vectors(
    rows: Sequence[sqlite3.Row], key: str
) -> tuple[tuple[str, tuple[float, ...]], ...]:
    result: list[tuple[str, tuple[float, ...]]] = []
    for row in rows:
        result.append(
            (
                str(row[key]),
                tuple(float(value) for value in json.loads(row["vector_json"])),
            )
        )
    return tuple(result)


def _cluster(row: sqlite3.Row) -> dict[str, Any]:
    value = _row(row)
    value["track_count"] = int(row["track_count"])
    value["session_count"] = int(row["session_count"])
    value["session_ids"] = sorted(
        value
        for value in str(row["session_ids_csv"] or "").split(",")
        if value
    )
    value.pop("session_ids_csv", None)
    confidence = row["suggestion_confidence"]
    value["suggestion_confidence"] = float(confidence) if confidence is not None else None
    link_confidence = row["link_confidence"]
    value["link_confidence"] = (
        float(link_confidence) if link_confidence is not None else None
    )
    return value


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys() if not key.endswith("_json")}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _contains_reference(value: object, reference_id: str) -> bool:
    if value == reference_id:
        return True
    if isinstance(value, dict):
        return any(_contains_reference(item, reference_id) for item in value.values())
    if isinstance(value, list):
        return any(_contains_reference(item, reference_id) for item in value)
    return False
