from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from allday_asr.v3.domain.models import Artifact
from allday_asr.v3.domain.identity import SelfIdentity, unknown_identity_evidence
from allday_asr.v3.domain.processing import ProcessingClaim


class StageCancellationRequested(RuntimeError):
    """Raised by an adapter after it reaches a durable cancellation checkpoint."""


@dataclass(frozen=True)
class ArtifactDependencyOutput:
    input_type: str
    input_id: str
    input_revision: int


@dataclass(frozen=True)
class StageArtifactOutput:
    kind: str
    payload: bytes
    producer: str
    producer_version: str
    metadata: dict[str, Any] = field(default_factory=dict)
    input_refs: tuple[str, ...] = ()
    dependencies: tuple[ArtifactDependencyOutput, ...] = ()


@dataclass(frozen=True)
class SpeakerProjectionOutput:
    label: str


@dataclass(frozen=True)
class UtteranceProjectionOutput:
    ordinal: int
    start_ms: int
    end_ms: int
    text: str
    speaker_label: str | None
    evidence: dict[str, Any]
    identity: SelfIdentity = SelfIdentity.UNKNOWN
    identity_evidence: dict[str, Any] = field(default_factory=unknown_identity_evidence)


@dataclass(frozen=True)
class StageExecutionContext:
    claim: ProcessingClaim
    prior_artifacts: dict[str, tuple[Artifact, bytes]]


@dataclass(frozen=True)
class StageExecutionResult:
    checkpoint: dict[str, Any] = field(default_factory=dict)
    log_summary: str = ""
    artifacts: tuple[StageArtifactOutput, ...] = ()
    speakers: tuple[SpeakerProjectionOutput, ...] = ()
    utterances: tuple[UtteranceProjectionOutput, ...] = ()


class StageExecutionControl(Protocol):
    def heartbeat(self, checkpoint: dict[str, Any] | None = None) -> bool: ...

    def cancellation_requested(self) -> bool: ...


class ProcessingStageAdapter(Protocol):
    def execute(
        self,
        context: StageExecutionContext,
        control: StageExecutionControl,
    ) -> StageExecutionResult: ...


__all__ = [
    "ArtifactDependencyOutput",
    "ProcessingStageAdapter",
    "SpeakerProjectionOutput",
    "StageArtifactOutput",
    "StageCancellationRequested",
    "StageExecutionContext",
    "StageExecutionControl",
    "StageExecutionResult",
    "UtteranceProjectionOutput",
]
