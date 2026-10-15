from __future__ import annotations

from typing import Any

from .generation_inputs import GenerationSourceConflict, validate_snapshot
from .reminder_edit_sources import edit_sources, validate_generation_sources

from allday_asr.v3.domain.knowledge import (
    EventOperationKind,
    GenerationSubmission,
    ProposalKind,
    ProposalStatus,
)
from allday_asr.v3.ports.repositories import UnitOfWork


class KnowledgeQueryMixin:
    def list_evidence(self, session_id: str) -> tuple[dict[str, Any], ...]:
        return self.materialize_evidence(session_id)

    def list_events(self, session_id: str) -> tuple[dict[str, Any], ...]:
        with self._uow_factory().reading() as uow:
            uow.catalog.get_session(session_id)
            return uow.knowledge.list_events(session_id)

    def event_history(self, event_id: str) -> tuple[dict[str, Any], ...]:
        with self._uow_factory().reading() as uow:
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
        with self._uow_factory().reading() as uow:
            if session_id is not None:
                uow.catalog.get_session(session_id)
            return uow.knowledge.list_memories(session_id, subject_type, subject_id)

    def proposal_sources_current(self, proposal_id):
        with self._uow_factory().reading() as uow:
            proposal = uow.knowledge.get_proposal(proposal_id)
            generation = uow.knowledge.get_generation(proposal.generation_id)
            try:
                validate_generation_sources(uow, generation, proposal)
            except (GenerationSourceConflict, KeyError):
                return False
            return True

    def list_proposals(
        self, status: str | None = None, limit: int = 100
    ) -> tuple[dict[str, Any], ...]:
        if status is not None:
            ProposalStatus(status)
        if not 1 <= limit <= 500:
            raise ValueError("proposal page limit must be between 1 and 500")
        with self._uow_factory().reading() as uow:
            return uow.knowledge.list_proposals(status, limit)

    def affected_by(self, source_type: str, source_id: str) -> dict[str, Any]:
        if not source_type.strip() or not source_id.strip():
            raise ValueError("derivation source is required")
        with self._uow_factory().reading() as uow:
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
        with self._uow_factory().reading() as uow:
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
        with self._uow_factory().reading() as uow:
            rows = uow.derivations.list_recompute_requests(status, limit)
            return tuple({**row, 'execution_mode': 'manual',
                          'action_required': 'regenerate_from_current_sources' if row['status'] in {'queued','failed'} else None}
                         for row in rows)

    def _resolve_inputs(
        self, uow: UnitOfWork, submission: GenerationSubmission
    ) -> dict[str, Any]:
        snapshot = submission.input_scope.get("input_snapshot")
        source_id, _ = edit_sources(uow, submission.producer, submission.model, submission.input_scope, submission.proposals)
        if snapshot is not None:
            validate_snapshot(uow, snapshot, generation_id=source_id)
        elif submission.producer.startswith("codex-"):
            raise GenerationSourceConflict("model generation requires actual input snapshot; regenerate")
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
            raise ValueError(
                f"proposal evidence utterance does not exist: {missing[0]}"
            )
        for kind, payload, proposal_evidence in submission.proposals:
            session_id = payload.get("session_id")
            if session_id is not None:
                uow.catalog.get_session(str(session_id))
            if kind is ProposalKind.EVENT_OPERATION:
                evidence_sessions = {
                    utterances[utterance_id][0] for utterance_id in proposal_evidence
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
        resolved = {"utterances": [
            {"utterance_id": i, "session_id": utterances[i][0], "revision": utterances[i][1]}
            for i in evidence_ids], "events": events}
        return {
            **submission.input_scope,
            "input_snapshot": snapshot or {"version": 1, "kind": "command", "inputs": resolved},
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
