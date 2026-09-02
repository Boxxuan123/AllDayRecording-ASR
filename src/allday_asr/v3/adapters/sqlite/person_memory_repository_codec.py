from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from typing import Any


def _event_summary(payload: dict[str, Any], fallback: str) -> str:
    for key in ("summary", "content", "text", "description", "title"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def _contains_reference(value: object, reference_id: str) -> bool:
    if value == reference_id:
        return True
    if isinstance(value, dict):
        return any(_contains_reference(item, reference_id) for item in value.values())
    if isinstance(value, list):
        return any(_contains_reference(item, reference_id) for item in value)
    return False


def _unique_text(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _row(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {key: row[key] for key in row.keys() if not key.endswith("_json")}
