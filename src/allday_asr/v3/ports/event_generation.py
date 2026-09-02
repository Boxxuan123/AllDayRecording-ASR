from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class SemanticEventReasoningEffort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


@dataclass(frozen=True)
class SemanticEventModelRequest:
    session_id: str
    captured_timezone: str
    utterances: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class SemanticEventModelResult:
    events: tuple[dict[str, Any], ...]
    turn_id: str
    reasoning_effort: SemanticEventReasoningEffort
    usage: dict[str, Any] = field(default_factory=dict)


class SemanticEventModelGenerator(Protocol):
    model_label: str
    producer_version: str
    prompt_version: str
    extractor_version: str

    def generate(
        self,
        request: SemanticEventModelRequest,
        effort: SemanticEventReasoningEffort,
    ) -> SemanticEventModelResult: ...

    def close(self) -> None: ...


__all__ = [
    "SemanticEventModelGenerator",
    "SemanticEventModelRequest",
    "SemanticEventModelResult",
    "SemanticEventReasoningEffort",
]
