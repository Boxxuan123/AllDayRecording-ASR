from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


DAILY_NARRATIVE_SECTIONS = (
    "what_happened",
    "decisions",
    "new_todos",
    "completed",
    "unresolved",
    "important_people_interactions",
    "memorable_quotes",
    "tomorrow_attention",
)


class InsightStatus(StrEnum):
    ACTIVE = "active"
    RETRACTED = "retracted"


@dataclass(frozen=True)
class InsightEvidenceReference:
    event_id: str | None = None
    utterance_id: str | None = None

    def __post_init__(self) -> None:
        if self.event_id is None and self.utterance_id is None:
            raise ValueError("insight evidence requires an event or utterance")
        if self.event_id is not None and not self.event_id.strip():
            raise ValueError("insight event evidence id is invalid")
        if self.utterance_id is not None and not self.utterance_id.strip():
            raise ValueError("insight utterance evidence id is invalid")


@dataclass(frozen=True)
class NarrativeItem:
    text: str
    evidence_event_ids: tuple[str, ...] = field(default_factory=tuple)
    evidence_utterance_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("narrative text is required")
        if not self.evidence_event_ids and not self.evidence_utterance_ids:
            raise ValueError("narrative item requires evidence")
        if len(set(self.evidence_event_ids)) != len(self.evidence_event_ids):
            raise ValueError("narrative event evidence must be unique")
        if len(set(self.evidence_utterance_ids)) != len(self.evidence_utterance_ids):
            raise ValueError("narrative utterance evidence must be unique")

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text.strip(),
            "evidence_event_ids": list(self.evidence_event_ids),
            "evidence_utterance_ids": list(self.evidence_utterance_ids),
        }


@dataclass(frozen=True)
class ModelObservation(NarrativeItem):
    confidence: float = 0.0
    rationale: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        if not 0 <= self.confidence <= 1:
            raise ValueError("observation confidence must be between zero and one")
        if not self.rationale.strip():
            raise ValueError("observation rationale is required")

    def as_dict(self) -> dict[str, Any]:
        return {
            **super().as_dict(),
            "confidence": self.confidence,
            "rationale": self.rationale.strip(),
        }


__all__ = [
    "DAILY_NARRATIVE_SECTIONS",
    "InsightEvidenceReference",
    "InsightStatus",
    "ModelObservation",
    "NarrativeItem",
]
