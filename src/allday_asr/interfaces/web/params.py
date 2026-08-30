from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs


def query_int(query: str, name: str) -> int:
    values = parse_qs(query).get(name)
    if not values:
        raise ValueError(f"缺少查询参数 {name}")
    value = int(values[0])
    if value < 1:
        raise ValueError(f"{name} 必须大于 0")
    return value


def query_optional_int(query: str, name: str) -> int | None:
    values = parse_qs(query).get(name)
    if not values:
        return None
    try:
        value = int(values[0])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"查询参数 {name} 必须是整数") from exc
    if value < 1:
        raise ValueError(f"{name} 必须大于 0")
    return value


def query_target(query: str) -> tuple[int | None, int | None]:
    recording_id = query_optional_int(query, "recording_id")
    session_id = query_optional_int(query, "session_id")
    if recording_id is None and session_id is None:
        raise ValueError("缺少查询参数 recording_id 或 session_id")
    return recording_id, session_id


def body_target(body: dict[str, Any]) -> tuple[int | None, int | None]:
    recording_value = body.get("recording_id")
    session_value = body.get("session_id")
    recording_id = int(recording_value) if recording_value is not None else None
    session_id = int(session_value) if session_value is not None else None
    if recording_id is None and session_id is None:
        raise ValueError("缺少 recording_id 或 session_id")
    if recording_id is not None and recording_id < 1:
        raise ValueError("recording_id 必须大于 0")
    if session_id is not None and session_id < 1:
        raise ValueError("session_id 必须大于 0")
    return recording_id, session_id


def query_nonnegative_int(query: str, name: str) -> int:
    values = parse_qs(query).get(name)
    if not values:
        raise ValueError(f"缺少查询参数 {name}")
    try:
        value = int(values[0])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"查询参数 {name} 必须是整数") from exc
    if value < 0:
        raise ValueError(f"查询参数 {name} 不能为负数")
    return value


def match_path(path: str, pattern: str) -> dict[str, str] | None:
    match = re.fullmatch(pattern, path)
    return match.groupdict() if match else None
