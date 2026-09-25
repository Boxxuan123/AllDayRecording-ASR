from __future__ import annotations
from typing import Any, Protocol
from allday_asr.v3.domain.knowledge import (
    DerivationDependency,
    EventCurrentState,
    EventOperation,
    EvidenceSpan,
    GenerationRecord,
    InvalidationEvent,
    MemoryRecord,
    RecomputeRequest,
    StructuredProposal,
)
from allday_asr.v3.domain.reminders import (
    ReminderCandidate,
    ReminderFeedback,
    ReminderSchedule,
)


class KnowledgeRepository(Protocol):
    def unmaterialized_evidence(
        self, session_id: str
    ) -> tuple[dict[str, Any], ...]: ...
    def add_evidence_span(self, span: EvidenceSpan) -> bool: ...
    def list_evidence(self, session_id: str) -> tuple[dict[str, Any], ...]: ...
    def next_generation_number(
        self,
        layer: str,
        producer: str,
        producer_version: str,
        model: str,
        prompt_version: str,
        extractor_version: str,
        input_sha256: str,
    ) -> int: ...
    def add_generation(self, generation: GenerationRecord) -> bool: ...
    def get_generation(self, generation_id: str) -> GenerationRecord: ...
    def complete_generation(
        self, generation_id: str, status: str, completed_at: str, error: str | None
    ) -> None: ...
    def add_proposal(self, proposal: StructuredProposal) -> bool: ...
    def get_proposal(self, proposal_id: str) -> StructuredProposal: ...
    def list_proposals(
        self, status: str | None, limit: int
    ) -> tuple[dict[str, Any], ...]: ...
    def resolve_proposal(
        self,
        proposal_id: str,
        status: str,
        resolved_at: str,
        resolved_by: str,
        reason: str | None,
    ) -> None: ...
    def utterance_revisions(
        self, utterance_ids: tuple[str, ...]
    ) -> dict[str, tuple[str, int]]: ...
    def utterance_facts(
        self, utterance_ids: tuple[str, ...]
    ) -> dict[str, dict[str, Any]]: ...
    def get_event(self, event_id: str) -> EventCurrentState | None: ...
    def add_event_operation(self, operation: EventOperation) -> bool: ...
    def put_event_state(
        self, state: EventCurrentState, expected_revision: int
    ) -> None: ...
    def list_events(self, session_id: str) -> tuple[dict[str, Any], ...]: ...
    def event_history(self, event_id: str) -> tuple[dict[str, Any], ...]: ...
    def next_memory_version(self, memory_id: str) -> int: ...
    def add_memory(self, memory: MemoryRecord) -> bool: ...
    def list_memories(
        self, session_id: str | None, subject_type: str | None, subject_id: str | None
    ) -> tuple[dict[str, Any], ...]: ...
    def add_evidence_link(
        self,
        link_id: str,
        subject_type: str,
        subject_id: str,
        subject_revision: int,
        evidence_type: str,
        evidence_id: str,
        evidence_revision: int,
        created_at: str,
    ) -> bool: ...


class DerivationRepository(Protocol):
    def add_dependency(self, dependency: DerivationDependency) -> bool: ...
    def dependent_closure(
        self, input_type: str, input_id: str
    ) -> tuple[tuple[str, str, int], ...]: ...
    def add_invalidation(self, event: InvalidationEvent) -> bool: ...
    def add_recompute_request(self, request: RecomputeRequest) -> bool: ...
    def complete_recompute_for_target(
        self,
        target_type: str,
        target_id: str,
        target_revision: int,
        generation_id: str,
        updated_at: str,
    ) -> int: ...
    def list_invalidations(
        self, target_type: str | None, target_id: str | None, limit: int
    ) -> tuple[dict[str, Any], ...]: ...
    def list_recompute_requests(
        self, status: str | None, limit: int
    ) -> tuple[dict[str, Any], ...]: ...


class ReminderRepository(Protocol):
    def invalidate_source_candidates(self, utterance_id: str, now: str) -> None: ...
    def has_user_task_for_evidence(self, utterance_ids) -> bool: ...
    def add_candidate(self, candidate: ReminderCandidate) -> bool: ...
    def get_candidate(self, candidate_id: str) -> ReminderCandidate: ...
    def get_candidate_by_proposal(
        self, proposal_id: str
    ) -> ReminderCandidate | None: ...
    def list_candidates(
        self, status: str | None, limit: int
    ) -> tuple[dict[str, Any], ...]: ...
    def duplicate_for(
        self, dedup_key: str, *, exclude_candidate_id: str | None = None
    ) -> dict[str, str] | None: ...
    def resolve_candidate(
        self,
        candidate_id: str,
        status: str,
        matched_event_id: str | None,
        conflict_reason: str | None,
        resolved_at: str,
        resolved_by: str,
    ) -> None: ...
    def get_schedule(self, event_id: str) -> ReminderSchedule | None: ...
    def put_schedule(self, schedule: ReminderSchedule) -> None: ...
    def list_schedules(
        self,
        *,
        status: str | None,
        session_id: str | None,
        due_before: str | None,
        limit: int,
    ) -> tuple[dict[str, Any], ...]: ...
    def mark_delivered(self, event_id: str, delivered_at: str) -> None: ...
    def mark_stale(
        self, event_id: str, event_revision: int, updated_at: str
    ) -> bool: ...
    def add_feedback(self, feedback: ReminderFeedback) -> bool: ...
    def list_feedback(self, candidate_id: str) -> tuple[dict[str, Any], ...]: ...
