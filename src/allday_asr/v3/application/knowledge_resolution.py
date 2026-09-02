from __future__ import annotations

from datetime import datetime
from typing import Any

from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.knowledge import (
    DerivationDependency,
    EventCurrentState,
    EventKind,
    EventOperation,
    EventOperationKind,
    EventStatus,
    InvalidationEvent,
    MemoryKind,
    MemoryRecord,
    ProposalStatus,
    StructuredProposal,
)
from allday_asr.v3.ports.repositories import UnitOfWork

from .knowledge_support import (
    ProposalResolution,
    _datetime,
    _merge_patch,
)


class KnowledgeResolutionMixin:
    def _accept_event(
        self,
        uow: UnitOfWork,
        proposal: StructuredProposal,
        actor: str,
        now: datetime,
    ) -> ProposalResolution:
        payload = proposal.payload
        operation_kind = EventOperationKind(payload["operation"])
        event_kind = EventKind(payload["event_kind"])
        expected_revision = int(payload["expected_revision"])
        event_id = str(payload.get("event_id") or new_ulid())
        current = uow.knowledge.get_event(event_id)
        if operation_kind is EventOperationKind.CREATE:
            if current is not None or expected_revision != 0:
                raise ValueError("event create revision conflict")
            revision = 1
            status = EventStatus.ACTIVE
            current_payload: dict[str, Any] = {}
            created_at = now
        else:
            if current is None:
                raise ValueError("event does not exist")
            if current.revision != expected_revision:
                raise ValueError("event revision conflict")
            if current.session_id != payload["session_id"]:
                raise ValueError("event session cannot change")
            if current.event_kind is not event_kind:
                raise ValueError("event kind cannot change")
            revision = current.revision + 1
            status = current.status
            current_payload = current.payload
            created_at = current.created_at
        if operation_kind is EventOperationKind.CANCEL:
            status = EventStatus.CANCELLED
        elif operation_kind is EventOperationKind.COMPLETE:
            status = EventStatus.COMPLETED
        elif operation_kind is EventOperationKind.REOPEN:
            status = EventStatus.ACTIVE
        elif (
            operation_kind is EventOperationKind.UPDATE
            and status is EventStatus.CANCELLED
        ):
            raise ValueError("cancelled event must be reopened before update")
        next_payload = _merge_patch(current_payload, payload["patch"])
        if not next_payload:
            raise ValueError("event current payload cannot be empty")
        operation = EventOperation(
            operation_id=new_ulid(),
            event_id=event_id,
            session_id=str(payload["session_id"]),
            event_kind=event_kind,
            operation=operation_kind,
            event_revision=revision,
            payload=dict(payload["patch"]),
            actor=actor,
            generation_id=proposal.generation_id,
            proposal_id=proposal.proposal_id,
            created_at=now,
        )
        if not uow.knowledge.add_event_operation(operation):
            raise ValueError("event operation conflicts with current history")
        state = EventCurrentState(
            event_id=event_id,
            session_id=operation.session_id,
            event_kind=event_kind,
            status=status,
            revision=revision,
            payload=next_payload,
            latest_operation_id=operation.operation_id,
            created_at=created_at,
            updated_at=now,
        )
        uow.knowledge.put_event_state(state, expected_revision)
        if current is not None:
            uow.derivations.complete_recompute_for_target(
                "event",
                event_id,
                current.revision,
                proposal.generation_id,
                _datetime(now),
            )
        if current is not None:
            uow.derivations.add_dependency(
                DerivationDependency(
                    dependent_type="event",
                    dependent_id=event_id,
                    dependent_revision=revision,
                    input_type="event",
                    input_id=event_id,
                    input_revision=current.revision,
                    created_at=now,
                )
            )
        revisions = uow.knowledge.utterance_revisions(proposal.evidence_utterance_ids)
        if len(revisions) != len(proposal.evidence_utterance_ids):
            raise ValueError("event evidence changed before acceptance")
        sessions = {value[0] for value in revisions.values()}
        if sessions != {operation.session_id}:
            raise ValueError("event evidence must belong to its session")
        for utterance_id in proposal.evidence_utterance_ids:
            utterance_revision = revisions[utterance_id][1]
            self._link_dependency(
                uow,
                "event",
                event_id,
                revision,
                "utterance",
                utterance_id,
                utterance_revision,
                now,
            )
        return ProposalResolution(
            proposal.proposal_id,
            ProposalStatus.ACCEPTED,
            "event",
            event_id,
            revision,
        )

    def _accept_memory(
        self,
        uow: UnitOfWork,
        proposal: StructuredProposal,
        actor: str,
        now: datetime,
    ) -> ProposalResolution:
        payload = proposal.payload
        memory_id = str(payload.get("memory_id") or new_ulid())
        version = uow.knowledge.next_memory_version(memory_id)
        event_states: list[EventCurrentState] = []
        for event_id in payload["input_event_ids"]:
            state = uow.knowledge.get_event(event_id)
            if state is None or state.derivation_status != "active":
                raise ValueError(f"memory input event is not active: {event_id}")
            event_states.append(state)
        if payload["session_id"] is not None and {
            state.session_id for state in event_states
        } != {payload["session_id"]}:
            raise ValueError("session memory inputs must belong to its session")
        memory = MemoryRecord(
            memory_id=memory_id,
            version=version,
            session_id=payload["session_id"],
            kind=MemoryKind(payload["memory_kind"]),
            subject_type=str(payload["subject_type"]),
            subject_id=str(payload["subject_id"]),
            content=dict(payload["content"]),
            generation_id=proposal.generation_id,
            created_at=now,
        )
        if not uow.knowledge.add_memory(memory):
            raise ValueError("memory version conflict")
        for state in event_states:
            self._link_dependency(
                uow,
                "memory",
                memory_id,
                version,
                "event",
                state.event_id,
                state.revision,
                now,
            )
        revisions = uow.knowledge.utterance_revisions(proposal.evidence_utterance_ids)
        if len(revisions) != len(proposal.evidence_utterance_ids):
            raise ValueError("memory evidence changed before acceptance")
        for utterance_id in proposal.evidence_utterance_ids:
            self._link_dependency(
                uow,
                "memory",
                memory_id,
                version,
                "utterance",
                utterance_id,
                revisions[utterance_id][1],
                now,
            )
        if version > 1:
            uow.derivations.complete_recompute_for_target(
                "memory",
                memory_id,
                version - 1,
                proposal.generation_id,
                _datetime(now),
            )
            cascade_root_id = new_ulid()
            superseded = InvalidationEvent(
                invalidation_id=new_ulid(),
                target_type="memory",
                target_id=memory_id,
                target_revision=version - 1,
                status="superseded",
                reason="new_memory_version_accepted",
                source_type="generation",
                source_id=proposal.generation_id,
                source_revision=1,
                cascade_root_id=cascade_root_id,
                created_at=now,
            )
            uow.derivations.add_invalidation(superseded)
        return ProposalResolution(
            proposal.proposal_id,
            ProposalStatus.ACCEPTED,
            "memory",
            memory_id,
            version,
        )

    @staticmethod
    def _link_dependency(
        uow: UnitOfWork,
        subject_type: str,
        subject_id: str,
        subject_revision: int,
        evidence_type: str,
        evidence_id: str,
        evidence_revision: int,
        now: datetime,
    ) -> None:
        uow.knowledge.add_evidence_link(
            new_ulid(),
            subject_type,
            subject_id,
            subject_revision,
            evidence_type,
            evidence_id,
            evidence_revision,
            _datetime(now),
        )
        uow.derivations.add_dependency(
            DerivationDependency(
                dependent_type=subject_type,
                dependent_id=subject_id,
                dependent_revision=subject_revision,
                input_type=evidence_type,
                input_id=evidence_id,
                input_revision=evidence_revision,
                created_at=now,
            )
        )
