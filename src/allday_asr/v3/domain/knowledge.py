from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class KnowledgeLayer(StrEnum):
    EVIDENCE = "evidence"
    EVENT = "event"
    MEMORY = "memory"


class GenerationStatus(StrEnum):
    COLLECTING = "collecting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STALE = "stale"


class ProposalKind(StrEnum):
    EVENT_OPERATION = "event_operation"
    MEMORY_RECORD = "memory_record"


class ProposalStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class EventKind(StrEnum):
    TASK = "task"
    REQUEST = "request"
    COMMITMENT = "commitment"
    APPOINTMENT = "appointment"
    DECISION = "decision"
    PERSON_FACT = "person_fact"
    IMPORTANT_EXPERIENCE = "important_experience"


class EventOperationKind(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    CANCEL = "cancel"
    COMPLETE = "complete"
    REOPEN = "reopen"


class EventStatus(StrEnum):
    ACTIVE = "active"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class MemoryKind(StrEnum):
    DAILY_SUMMARY = "daily_summary"
    PERSON_MEMORY = "person_memory"
    PERSON_FACT = "person_fact"
    RELATIONSHIP_OBSERVATION = "relationship_observation"
    INTERACTION_STATISTIC = "interaction_statistic"
    MODEL_INFERENCE = "model_inference"


@dataclass(frozen=True)
class EvidenceSpan:
    evidence_span_id: str
    session_id: str
    asset_id: str
    artifact_id: str
    session_start_ms: int
    session_end_ms: int
    asset_start_ms: int
    asset_end_ms: int
    created_at: datetime
    utterance_id: str | None = None

    def __post_init__(self) -> None:
        if min(self.session_start_ms, self.asset_start_ms) < 0:
            raise ValueError("evidence span coordinates cannot be negative")
        if self.session_end_ms <= self.session_start_ms:
            raise ValueError("evidence session range must be non-empty")
        if self.asset_end_ms <= self.asset_start_ms:
            raise ValueError("evidence asset range must be non-empty")


@dataclass(frozen=True)
class GenerationRecord:
    generation_id: str
    layer: KnowledgeLayer
    producer: str
    producer_version: str
    model: str
    prompt_version: str
    extractor_version: str
    input_scope: dict[str, Any]
    input_sha256: str
    generation_number: int
    status: GenerationStatus
    created_at: datetime
    completed_at: datetime | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("producer", self.producer),
            ("producer_version", self.producer_version),
            ("model", self.model),
            ("prompt_version", self.prompt_version),
            ("extractor_version", self.extractor_version),
        ):
            if not value.strip():
                raise ValueError(f"generation {label} is required")
        if len(self.input_sha256) != 64:
            raise ValueError("generation input digest must be SHA-256")
        int(self.input_sha256, 16)
        if self.generation_number < 1:
            raise ValueError("generation number must be positive")


@dataclass(frozen=True)
class StructuredProposal:
    proposal_id: str
    generation_id: str
    kind: ProposalKind
    payload: dict[str, Any]
    evidence_utterance_ids: tuple[str, ...]
    status: ProposalStatus
    created_at: datetime
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    resolution_reason: str | None = None

    def __post_init__(self) -> None:
        if len(set(self.evidence_utterance_ids)) != len(
            self.evidence_utterance_ids
        ):
            raise ValueError("proposal evidence utterance ids must be unique")
        if self.status is ProposalStatus.PENDING and any(
            value is not None
            for value in (self.resolved_at, self.resolved_by, self.resolution_reason)
        ):
            raise ValueError("pending proposal cannot have a resolution")


@dataclass(frozen=True)
class EventOperation:
    operation_id: str
    event_id: str
    session_id: str
    event_kind: EventKind
    operation: EventOperationKind
    event_revision: int
    payload: dict[str, Any]
    actor: str
    generation_id: str
    proposal_id: str
    created_at: datetime

    def __post_init__(self) -> None:
        if self.event_revision < 1:
            raise ValueError("event revision must be positive")
        if not self.actor.strip():
            raise ValueError("event operation actor is required")


@dataclass(frozen=True)
class EventCurrentState:
    event_id: str
    session_id: str
    event_kind: EventKind
    status: EventStatus
    revision: int
    payload: dict[str, Any]
    latest_operation_id: str
    created_at: datetime
    updated_at: datetime
    derivation_status: str = "active"

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ValueError("event state revision must be positive")


@dataclass(frozen=True)
class MemoryRecord:
    memory_id: str
    version: int
    session_id: str | None
    kind: MemoryKind
    subject_type: str
    subject_id: str
    content: dict[str, Any]
    generation_id: str
    created_at: datetime
    derivation_status: str = "active"

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("memory version must be positive")
        if not self.subject_type.strip() or not self.subject_id.strip():
            raise ValueError("memory subject is required")


@dataclass(frozen=True)
class DerivationDependency:
    dependent_type: str
    dependent_id: str
    dependent_revision: int
    input_type: str
    input_id: str
    input_revision: int
    created_at: datetime

    def __post_init__(self) -> None:
        if self.dependent_revision < 1 or self.input_revision < 1:
            raise ValueError("derivation dependency revisions must be positive")


@dataclass(frozen=True)
class InvalidationEvent:
    invalidation_id: str
    target_type: str
    target_id: str
    target_revision: int
    status: str
    reason: str
    source_type: str
    source_id: str
    source_revision: int
    cascade_root_id: str
    created_at: datetime


@dataclass(frozen=True)
class RecomputeRequest:
    request_id: str
    target_type: str
    target_id: str
    target_revision: int
    invalidation_id: str
    status: str
    reason: str
    created_at: datetime
    updated_at: datetime
    generation_id: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class GenerationSubmission:
    layer: KnowledgeLayer
    producer: str
    producer_version: str
    model: str
    prompt_version: str
    extractor_version: str
    input_scope: dict[str, Any]
    proposals: tuple[tuple[ProposalKind, dict[str, Any], tuple[str, ...]], ...] = field(
        default_factory=tuple
    )


__all__ = [
    "DerivationDependency",
    "EventCurrentState",
    "EventKind",
    "EventOperation",
    "EventOperationKind",
    "EventStatus",
    "EvidenceSpan",
    "GenerationRecord",
    "GenerationStatus",
    "GenerationSubmission",
    "InvalidationEvent",
    "KnowledgeLayer",
    "MemoryKind",
    "MemoryRecord",
    "ProposalKind",
    "ProposalStatus",
    "RecomputeRequest",
    "StructuredProposal",
]
