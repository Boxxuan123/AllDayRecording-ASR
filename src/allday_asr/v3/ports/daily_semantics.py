"""Bounded semantic analysis; no identity/task authority and no full-day input."""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class DailySemanticResult:
    segments: tuple[dict, ...]
    provenance: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DailyReconciliationResult:
    groups: tuple[dict, ...]
    provenance: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DailyOverviewResult:
    content: dict
    provenance: dict = field(default_factory=dict)


class DailySemanticAnalyzer(Protocol):
    model_label: str
    prompt_version: str
    producer_version: str
    provider: str
    reconcile_prompt_version: str
    normalization_prompt_version: str
    overview_prompt_version: str

    def analyze(self, request: dict) -> DailySemanticResult: ...
    def reconcile(self, request: dict) -> DailyReconciliationResult: ...
    def normalize(self, request: dict) -> DailyReconciliationResult: ...
    def overview(self, request: dict) -> DailyOverviewResult: ...
    def close(self) -> None: ...
