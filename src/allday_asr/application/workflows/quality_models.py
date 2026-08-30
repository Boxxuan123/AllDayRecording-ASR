from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class WorkflowContext:
    workflow_run_id: int
    session_id: int
    recording_id: int | None
    input_fingerprint: str
    admission_mode: str


@dataclass(frozen=True)
class ReviewReason:
    code: str
    stage: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "stage": self.stage,
            "message": self.message,
            **self.details,
        }


@dataclass(frozen=True)
class StageResult:
    stage: str
    run_id: int | None
    status: str
    reused: bool
    summary: Mapping[str, Any]
    review_reasons: tuple[ReviewReason, ...] = ()


@dataclass(frozen=True)
class WorkflowStageResults:
    admission: StageResult
    asr: StageResult
    diarization: StageResult
    speech_recall: StageResult
    identity_audit: StageResult
    identity_mining: StageResult
    semantic: StageResult

    @property
    def review_reasons(self) -> tuple[ReviewReason, ...]:
        return tuple(
            reason
            for result in (
                self.speech_recall,
                self.identity_audit,
                self.identity_mining,
            )
            for reason in result.review_reasons
        )

    @property
    def reused_stages(self) -> tuple[str, ...]:
        return tuple(
            result.stage
            for result in (
                self.asr,
                self.diarization,
                self.speech_recall,
                self.identity_audit,
                self.semantic,
            )
            if result.reused
        )


@dataclass(frozen=True)
class QualityWorkflowSummary:
    workflow_run_id: int
    session_id: int
    recording_id: int | None
    state: str
    asr_run_id: int
    diarization_run_id: int
    v2d1_run_id: int | None
    v2d2_run_id: int | None
    semantic_run_id: int | None
    review_required: bool
    review_reasons: tuple[dict[str, Any], ...]
    reused_stages: tuple[str, ...]
