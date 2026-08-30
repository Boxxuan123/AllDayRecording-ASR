from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def absolute_timestamp(
    recorded_at: str, offset_ms: int, timezone_name: str | None = None
) -> str | None:
    value = absolute_datetime(recorded_at, offset_ms, timezone_name)
    return value.isoformat() if value else None


def absolute_datetime(
    recorded_at: str, offset_ms: int, timezone_name: str | None = None
) -> datetime | None:
    try:
        base = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    value = base + timedelta(milliseconds=offset_ms)
    if timezone_name:
        try:
            value = value.astimezone(ZoneInfo(timezone_name))
        except ZoneInfoNotFoundError:
            pass
    return value


def format_offset(milliseconds: int) -> str:
    seconds = milliseconds // 1000
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def format_clock(recording: Any, milliseconds: int) -> str:
    value = absolute_datetime(
        recording["recorded_at"], milliseconds, recording["timezone"]
    )
    return value.strftime("%H:%M:%S") if value else format_offset(milliseconds)
