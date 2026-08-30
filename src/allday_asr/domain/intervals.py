from __future__ import annotations

from collections.abc import Iterable
from typing import Any, TypeVar


Segment = TypeVar("Segment")


def group_segments(
    segments: Iterable[Segment], max_gap_ms: int
) -> list[list[Segment]]:
    """Group ordered time ranges when the gap does not exceed the threshold."""
    groups: list[list[Segment]] = []
    for segment in segments:
        if (
            not groups
            or int(_field(segment, "start_ms"))
            - int(_field(groups[-1][-1], "end_ms"))
            > max_gap_ms
        ):
            groups.append([segment])
        else:
            groups[-1].append(segment)
    return groups


def _field(value: Any, key: str) -> Any:
    return value[key]
