from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class InsightReasoningEffort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


@dataclass(frozen=True)
class DailyInsightModelRequest:
    summary_date: str
    timezone: str
    objective: dict[str, Any]
    source_events: tuple[dict[str, Any], ...]
    key_quotes: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class RelationshipInsightModelRequest:
    person: dict[str, Any]
    window_days: int
    end_date: str
    timezone: str
    verified_facts: dict[str, Any]
    source_events: tuple[dict[str, Any], ...]
    interactions: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class DailyInsightModelResult:
    narrative: dict[str, tuple[dict[str, Any], ...]]
    turn_id: str
    reasoning_effort: InsightReasoningEffort
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RelationshipInsightModelResult:
    observations: tuple[dict[str, Any], ...]
    turn_id: str
    reasoning_effort: InsightReasoningEffort
    usage: dict[str, Any] = field(default_factory=dict)


class InsightModelGenerator(Protocol):
    model_label: str
    producer_version: str
    prompt_version: str
    extractor_version: str

    def generate_daily(
        self, request: DailyInsightModelRequest, effort: InsightReasoningEffort
    ) -> DailyInsightModelResult: ...

    def generate_relationship(
        self,
        request: RelationshipInsightModelRequest,
        effort: InsightReasoningEffort,
    ) -> RelationshipInsightModelResult: ...

    def close(self) -> None: ...


__all__ = [
    "DailyInsightModelRequest",
    "DailyInsightModelResult",
    "InsightModelGenerator",
    "InsightReasoningEffort",
    "RelationshipInsightModelRequest",
    "RelationshipInsightModelResult",
]
