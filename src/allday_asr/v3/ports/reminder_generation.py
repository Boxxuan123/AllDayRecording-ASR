from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class ReminderReasoningEffort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


@dataclass(frozen=True)
class ReminderModelRequest:
    session_id: str
    captured_timezone: str
    now_utc: str
    utterances: tuple[dict[str, Any], ...]
    active_reminders: tuple[dict[str, Any], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ReminderModelResult:
    intents: tuple[dict[str, Any], ...]
    turn_id: str
    reasoning_effort: ReminderReasoningEffort
    usage: dict[str, Any] = field(default_factory=dict)


class ReminderModelGenerator(Protocol):
    model_label: str
    producer_version: str
    prompt_version: str
    extractor_version: str

    def generate(
        self,
        request: ReminderModelRequest,
        effort: ReminderReasoningEffort,
    ) -> ReminderModelResult: ...

    def close(self) -> None: ...


__all__ = [
    "ReminderModelGenerator",
    "ReminderModelRequest",
    "ReminderModelResult",
    "ReminderReasoningEffort",
]
