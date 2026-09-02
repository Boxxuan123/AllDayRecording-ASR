from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from allday_asr.v3.domain.hashing import canonical_json_sha256
from allday_asr.v3.domain.ids import new_ulid, stable_ulid
from allday_asr.v3.domain.knowledge import (
    DerivationDependency,
    EventCurrentState,
    EventKind,
    EventOperation,
    EventOperationKind,
    EventStatus,
    EvidenceSpan,
    GenerationRecord,
    GenerationStatus,
    GenerationSubmission,
    InvalidationEvent,
    KnowledgeLayer,
    MemoryKind,
    MemoryRecord,
    ProposalKind,
    ProposalStatus,
    RecomputeRequest,
    StructuredProposal,
)
from allday_asr.v3.ports.repositories import UnitOfWork


UnitOfWorkFactory = Callable[[], UnitOfWork]
DateTimeClock = Callable[[], datetime]
ProposalCreatedHook = Callable[
    [UnitOfWork, GenerationRecord, StructuredProposal, int, datetime], None
]
ProposalAcceptedHook = Callable[
    [UnitOfWork, StructuredProposal, "ProposalResolution", datetime], None
]
ProposalRejectedHook = Callable[[UnitOfWork, StructuredProposal, datetime], None]


@dataclass(frozen=True)
class ProposalResolution:
    proposal_id: str
    status: ProposalStatus
    resource_type: str | None
    resource_id: str | None
    resource_revision: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "status": self.status.value,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "resource_revision": self.resource_revision,
        }


class KnowledgeArchitectureService:
    """V3.2 command boundary for derived event and memory data."""

    def __init__(
        self, uow_factory: UnitOfWorkFactory, *, now: DateTimeClock | None = None
    ) -> None:
        self._uow_factory = uow_factory
        self._now = now or _utc_now

    def materialize_evidence(self, session_id: str) -> tuple[dict[str, Any], ...]:
        now = self._now()
        with self._uow_factory() as uow:
            uow.catalog.get_session(session_id)
            for value in uow.knowledge.unmaterialized_evidence(session_id):
                span = EvidenceSpan(
                    evidence_span_id=stable_ulid(
                        "evidence-span",
                        value["utterance_id"],
                        value["asset_id"],
                        value["session_start_ms"],
                        value["session_end_ms"],
                    ),
                    session_id=str(value["session_id"]),
                    asset_id=str(value["asset_id"]),
                    artifact_id=str(value["source_artifact_id"]),
                    utterance_id=str(value["utterance_id"]),
                    session_start_ms=int(value["session_start_ms"]),
                    session_end_ms=int(value["session_end_ms"]),
                    asset_start_ms=int(value["asset_start_ms"]),
                    asset_end_ms=int(value["asset_end_ms"]),
                    created_at=now,
                )
                uow.knowledge.add_evidence_span(span)
            return uow.knowledge.list_evidence(session_id)

    def submit_generation(
        self,
        submission: GenerationSubmission,
        *,
        on_proposal_created: ProposalCreatedHook | None = None,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        now = self._now()
        _validate_submission(submission, allow_empty=allow_empty)
        with self._uow_factory() as uow:
            normalized_scope = self._resolve_inputs(uow, submission)
            input_sha256 = canonical_json_sha256(normalized_scope)
            generation_number = uow.knowledge.next_generation_number(
                submission.layer.value,
                submission.producer,
                submission.producer_version,
                submission.model,
                submission.prompt_version,
                submission.extractor_version,
                input_sha256,
            )
            generation = GenerationRecord(
                generation_id=new_ulid(),
                layer=submission.layer,
                producer=submission.producer,
                producer_version=submission.producer_version,
                model=submission.model,
                prompt_version=submission.prompt_version,
                extractor_version=submission.extractor_version,
                input_scope=normalized_scope,
                input_sha256=input_sha256,
                generation_number=generation_number,
                status=GenerationStatus.COLLECTING,
                created_at=now,
            )
            if not uow.knowledge.add_generation(generation):
                raise RuntimeError("generation identity collision")
            proposals: list[StructuredProposal] = []
            for index, (kind, payload, evidence_ids) in enumerate(
                submission.proposals
            ):
                proposal = StructuredProposal(
                    proposal_id=new_ulid(),
                    generation_id=generation.generation_id,
                    kind=kind,
                    payload=dict(payload),
                    evidence_utterance_ids=tuple(evidence_ids),
                    status=ProposalStatus.PENDING,
                    created_at=now,
                )
                if not uow.knowledge.add_proposal(proposal):
                    raise RuntimeError("proposal identity collision")
                proposals.append(proposal)
                if on_proposal_created is not None:
                    on_proposal_created(uow, generation, proposal, index, now)
            completed_at = _datetime(now)
            uow.knowledge.complete_generation(
                generation.generation_id,
                GenerationStatus.SUCCEEDED.value,
                completed_at,
                None,
            )
            uow.audit.append(
                "knowledge.generation.submitted",
                f"producer:{submission.producer}",
                "generation",
                generation.generation_id,
                {
                    "layer": submission.layer.value,
                    "input_sha256": input_sha256,
                    "generation_number": generation_number,
                    "proposal_count": len(proposals),
                },
            )
            return {
                "generation_id": generation.generation_id,
                "layer": generation.layer.value,
                "input_sha256": generation.input_sha256,
                "generation_number": generation.generation_number,
                "status": GenerationStatus.SUCCEEDED.value,
                "proposals": [
                    {
                        "proposal_id": proposal.proposal_id,
                        "kind": proposal.kind.value,
                        "status": proposal.status.value,
                    }
                    for proposal in proposals
                ],
            }

    def accept_proposal(
        self,
        proposal_id: str,
        actor: str,
        *,
        on_accepted: ProposalAcceptedHook | None = None,
    ) -> ProposalResolution:
        if not actor.strip():
            raise ValueError("proposal resolver is required")
        now = self._now()
        with self._uow_factory() as uow:
            proposal = uow.knowledge.get_proposal(proposal_id)
            if proposal.status is not ProposalStatus.PENDING:
                raise ValueError("proposal is no longer pending")
            generation = uow.knowledge.get_generation(proposal.generation_id)
            if generation.status is not GenerationStatus.SUCCEEDED:
                raise ValueError("proposal generation is not usable")
            if proposal.kind is ProposalKind.EVENT_OPERATION:
                resolution = self._accept_event(uow, proposal, actor, now)
            else:
                resolution = self._accept_memory(uow, proposal, actor, now)
            uow.knowledge.resolve_proposal(
                proposal_id,
                ProposalStatus.ACCEPTED.value,
                _datetime(now),
                actor,
                None,
            )
            if on_accepted is not None:
                on_accepted(uow, proposal, resolution, now)
            uow.audit.append(
                "knowledge.proposal.accepted",
                actor,
                "proposal",
                proposal_id,
                resolution.as_dict(),
            )
            return resolution

    def reject_proposal(
        self,
        proposal_id: str,
        actor: str,
        reason: str,
        *,
        on_rejected: ProposalRejectedHook | None = None,
    ) -> ProposalResolution:
        if not actor.strip() or not reason.strip():
            raise ValueError("proposal rejection requires actor and reason")
        now = self._now()
        with self._uow_factory() as uow:
            proposal = uow.knowledge.get_proposal(proposal_id)
            if proposal.status is not ProposalStatus.PENDING:
                raise ValueError("proposal is no longer pending")
            uow.knowledge.resolve_proposal(
                proposal_id,
                ProposalStatus.REJECTED.value,
                _datetime(now),
                actor,
                reason.strip(),
            )
            if on_rejected is not None:
                on_rejected(uow, proposal, now)
            uow.audit.append(
                "knowledge.proposal.rejected",
                actor,
                "proposal",
                proposal_id,
                {"reason": reason.strip()},
            )
        return ProposalResolution(
            proposal_id,
            ProposalStatus.REJECTED,
            None,
            None,
            None,
        )

    def list_evidence(self, session_id: str) -> tuple[dict[str, Any], ...]:
        return self.materialize_evidence(session_id)

    def list_events(self, session_id: str) -> tuple[dict[str, Any], ...]:
        with self._uow_factory() as uow:
            uow.catalog.get_session(session_id)
            return uow.knowledge.list_events(session_id)

    def event_history(self, event_id: str) -> tuple[dict[str, Any], ...]:
        with self._uow_factory() as uow:
            return uow.knowledge.event_history(event_id)

    def list_memories(
        self,
        *,
        session_id: str | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
    ) -> tuple[dict[str, Any], ...]:
        if (subject_type is None) != (subject_id is None):
            raise ValueError("memory subject type and id must be provided together")
        if session_id is None and subject_id is None:
            raise ValueError("memory query requires a session or subject")
        with self._uow_factory() as uow:
            if session_id is not None:
                uow.catalog.get_session(session_id)
            return uow.knowledge.list_memories(session_id, subject_type, subject_id)

    def list_proposals(
        self, status: str | None = None, limit: int = 100
    ) -> tuple[dict[str, Any], ...]:
        if status is not None:
            ProposalStatus(status)
        if not 1 <= limit <= 500:
            raise ValueError("proposal page limit must be between 1 and 500")
        with self._uow_factory() as uow:
            return uow.knowledge.list_proposals(status, limit)

    def affected_by(self, source_type: str, source_id: str) -> dict[str, Any]:
        if not source_type.strip() or not source_id.strip():
            raise ValueError("derivation source is required")
        with self._uow_factory() as uow:
            affected = uow.derivations.dependent_closure(source_type, source_id)
            return {
                "source": {"type": source_type, "id": source_id},
                "affected": [
                    {"type": kind, "id": identifier, "revision": revision}
                    for kind, identifier, revision in affected
                ],
            }

    def list_invalidations(
        self,
        target_type: str | None = None,
        target_id: str | None = None,
        limit: int = 100,
    ) -> tuple[dict[str, Any], ...]:
        if not 1 <= limit <= 500:
            raise ValueError("invalidation page limit must be between 1 and 500")
        with self._uow_factory() as uow:
            return uow.derivations.list_invalidations(target_type, target_id, limit)

    def list_recompute_requests(
        self, status: str | None = None, limit: int = 100
    ) -> tuple[dict[str, Any], ...]:
        if status is not None and status not in {
            "queued",
            "running",
            "succeeded",
            "failed",
            "cancelled",
        }:
            raise ValueError("recompute status is invalid")
        if not 1 <= limit <= 500:
            raise ValueError("recompute page limit must be between 1 and 500")
        with self._uow_factory() as uow:
            return uow.derivations.list_recompute_requests(status, limit)

    def _resolve_inputs(
        self, uow: UnitOfWork, submission: GenerationSubmission
    ) -> dict[str, Any]:
        evidence_ids = tuple(
            dict.fromkeys(
                utterance_id
                for _, _, proposal_evidence in submission.proposals
                for utterance_id in proposal_evidence
            )
        )
        utterances = uow.knowledge.utterance_revisions(evidence_ids)
        missing = sorted(set(evidence_ids) - set(utterances))
        if missing:
            raise ValueError(f"proposal evidence utterance does not exist: {missing[0]}")
        for kind, payload, proposal_evidence in submission.proposals:
            session_id = payload.get("session_id")
            if session_id is not None:
                uow.catalog.get_session(str(session_id))
            if kind is ProposalKind.EVENT_OPERATION:
                evidence_sessions = {
                    utterances[utterance_id][0]
                    for utterance_id in proposal_evidence
                }
                if evidence_sessions != {session_id}:
                    raise ValueError("event evidence must belong to its session")
                operation = EventOperationKind(payload["operation"])
                event_id = payload.get("event_id")
                current = (
                    uow.knowledge.get_event(str(event_id))
                    if event_id is not None
                    else None
                )
                if operation is EventOperationKind.CREATE:
                    if current is not None:
                        raise ValueError("event already exists")
                elif (
                    current is None
                    or current.session_id != session_id
                    or current.event_kind.value != payload["event_kind"]
                    or current.revision != payload["expected_revision"]
                ):
                    raise ValueError("event proposal does not match current revision")
        event_ids = tuple(
            dict.fromkeys(
                event_id
                for kind, payload, _ in submission.proposals
                if kind is ProposalKind.MEMORY_RECORD
                for event_id in payload["input_event_ids"]
            )
        )
        events: list[dict[str, Any]] = []
        for event_id in event_ids:
            state = uow.knowledge.get_event(event_id)
            if state is None:
                raise ValueError(f"memory input event does not exist: {event_id}")
            if state.derivation_status != "active":
                raise ValueError(f"memory input event is stale: {event_id}")
            events.append({"event_id": event_id, "revision": state.revision})
        return {
            **submission.input_scope,
            "resolved_inputs": {
                "utterances": [
                    {
                        "utterance_id": identifier,
                        "session_id": utterances[identifier][0],
                        "revision": utterances[identifier][1],
                    }
                    for identifier in evidence_ids
                ],
                "events": events,
            },
        }

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
        elif operation_kind is EventOperationKind.UPDATE and status is EventStatus.CANCELLED:
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
        revisions = uow.knowledge.utterance_revisions(
            proposal.evidence_utterance_ids
        )
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
        revisions = uow.knowledge.utterance_revisions(
            proposal.evidence_utterance_ids
        )
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


def cascade_derivations(
    uow: UnitOfWork,
    *,
    source_type: str,
    source_id: str,
    source_revision: int,
    reason: str,
    now: datetime,
) -> tuple[InvalidationEvent, ...]:
    root_id = new_ulid()
    created: list[InvalidationEvent] = []
    for target_type, target_id, target_revision in uow.derivations.dependent_closure(
        source_type, source_id
    ):
        event = InvalidationEvent(
            invalidation_id=new_ulid(),
            target_type=target_type,
            target_id=target_id,
            target_revision=target_revision,
            status="stale",
            reason=reason,
            source_type=source_type,
            source_id=source_id,
            source_revision=source_revision,
            cascade_root_id=root_id,
            created_at=now,
        )
        if not uow.derivations.add_invalidation(event):
            continue
        created.append(event)
        if target_type == "event" and uow.reminders.mark_stale(
            target_id, target_revision, _datetime(now)
        ):
            uow.changes.append(
                "reminder",
                target_id,
                target_revision,
                "tombstone",
                None,
            )
        if target_type in {"event", "memory"}:
            uow.derivations.add_recompute_request(
                RecomputeRequest(
                    request_id=new_ulid(),
                    target_type=target_type,
                    target_id=target_id,
                    target_revision=target_revision,
                    invalidation_id=event.invalidation_id,
                    status="queued",
                    reason=reason,
                    created_at=now,
                    updated_at=now,
                )
            )
    return tuple(created)


def _validate_submission(
    submission: GenerationSubmission, *, allow_empty: bool = False
) -> None:
    if not isinstance(submission.input_scope, dict) or not submission.input_scope:
        raise ValueError("generation input scope is required")
    if "resolved_inputs" in submission.input_scope:
        raise ValueError("resolved_inputs is reserved for the server")
    if not submission.proposals and not allow_empty:
        raise ValueError("generation must contain at least one proposal")
    if submission.layer is KnowledgeLayer.EVIDENCE:
        raise ValueError("evidence projections are not submitted as model proposals")
    for kind, payload, evidence_ids in submission.proposals:
        if not isinstance(kind, ProposalKind) or not isinstance(payload, dict):
            raise ValueError("generation proposal is invalid")
        if any(not isinstance(value, str) or not value for value in evidence_ids):
            raise ValueError("proposal evidence ids are invalid")
        if kind is ProposalKind.EVENT_OPERATION:
            _validate_event_proposal(payload, evidence_ids)
        else:
            _validate_memory_proposal(payload)
    if submission.layer is KnowledgeLayer.EVENT and any(
        kind is not ProposalKind.EVENT_OPERATION
        for kind, _, _ in submission.proposals
    ):
        raise ValueError("event generation can only submit event operations")
    if submission.layer is KnowledgeLayer.MEMORY and any(
        kind is not ProposalKind.MEMORY_RECORD for kind, _, _ in submission.proposals
    ):
        raise ValueError("memory generation can only submit memory records")


def _validate_event_proposal(
    payload: dict[str, Any], evidence_ids: tuple[str, ...]
) -> None:
    required = {
        "operation",
        "session_id",
        "event_kind",
        "expected_revision",
        "patch",
    }
    allowed = required | {"event_id"}
    if set(payload) - allowed or not required.issubset(payload):
        raise ValueError("event proposal fields are invalid")
    operation = EventOperationKind(payload["operation"])
    EventKind(payload["event_kind"])
    revision = payload["expected_revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("event expected revision is invalid")
    if operation is EventOperationKind.CREATE and revision != 0:
        raise ValueError("event create expected revision must be zero")
    if operation is not EventOperationKind.CREATE and revision < 1:
        raise ValueError("event mutation expected revision must be positive")
    if not isinstance(payload["session_id"], str) or not payload["session_id"]:
        raise ValueError("event session id is required")
    if "event_id" in payload and (
        not isinstance(payload["event_id"], str) or not payload["event_id"]
    ):
        raise ValueError("event id is invalid")
    if operation is not EventOperationKind.CREATE and "event_id" not in payload:
        raise ValueError("event mutation requires event id")
    if not isinstance(payload["patch"], dict):
        raise ValueError("event patch must be an object")
    if not evidence_ids:
        raise ValueError("every event operation requires utterance evidence")


def _validate_memory_proposal(payload: dict[str, Any]) -> None:
    required = {
        "session_id",
        "memory_kind",
        "subject_type",
        "subject_id",
        "content",
        "input_event_ids",
    }
    allowed = required | {"memory_id"}
    if set(payload) - allowed or not required.issubset(payload):
        raise ValueError("memory proposal fields are invalid")
    MemoryKind(payload["memory_kind"])
    if payload["session_id"] is not None and (
        not isinstance(payload["session_id"], str) or not payload["session_id"]
    ):
        raise ValueError("memory session id is invalid")
    for key in ("subject_type", "subject_id"):
        if not isinstance(payload[key], str) or not payload[key]:
            raise ValueError("memory subject is invalid")
    if not isinstance(payload["content"], dict) or not payload["content"]:
        raise ValueError("memory content is required")
    event_ids = payload["input_event_ids"]
    if (
        not isinstance(event_ids, list)
        or not event_ids
        or any(not isinstance(value, str) or not value for value in event_ids)
        or len(set(event_ids)) != len(event_ids)
    ):
        raise ValueError("memory input events are invalid")
    if "memory_id" in payload and (
        not isinstance(payload["memory_id"], str) or not payload["memory_id"]
    ):
        raise ValueError("memory id is invalid")


def _merge_patch(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = dict(current)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_patch(result[key], value)
        else:
            result[key] = value
    return result


def _datetime(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "KnowledgeArchitectureService",
    "ProposalResolution",
    "cascade_derivations",
]
