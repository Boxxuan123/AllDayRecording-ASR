from __future__ import annotations
from collections.abc import Callable
from datetime import datetime, timezone

Clock = Callable[[], str]


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
