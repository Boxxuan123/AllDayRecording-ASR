from __future__ import annotations

from datetime import datetime
from typing import Any

from allday_asr.v3.domain.knowledge import (
    ProposalKind,
    StructuredProposal,
)
from allday_asr.v3.domain.reminders import (
    ReminderCandidate,
    ReminderCandidateStatus,
    ReminderFeedbackAction,
    ReminderIntent,
    ReminderOperation,
    ReminderScheduleStatus,
)
from allday_asr.v3.ports.repositories import UnitOfWork

from .reminder_support import (
    AUTO_APPLY_CONFIDENCE,
    _datetime,
    _event_operation,
    _event_patch,
    _validate_limit,
)


class ReminderQueryMixin:
    def list_candidates(
        self, status: str | None = None, limit: int = 100
    ) -> tuple[dict[str, Any], ...]:
        if status is not None:
            ReminderCandidateStatus(status)
        _validate_limit(limit)
        with self._uow_factory() as uow:
            items = list(uow.reminders.list_candidates(status, limit))
        evidence_by_session: dict[str, tuple[dict[str, Any], ...]] = {}
        for item in items:
            session_id = str(item["session_id"])
            if session_id not in evidence_by_session:
                evidence_by_session[session_id] = self._knowledge.list_evidence(
                    session_id
                )
            evidence_ids = set(item["evidence_utterance_ids"])
            item["evidence"] = [
                span
                for span in evidence_by_session[session_id]
                if span["utterance_id"] in evidence_ids
            ]
        return tuple(items)

    def candidate(self, candidate_id: str) -> dict[str, Any]:
        for item in self.list_candidates(limit=500):
            if item["candidate_id"] == candidate_id:
                return item
        raise KeyError(f"reminder candidate does not exist: {candidate_id}")

    def list_schedules(
        self,
        *,
        status: str | None = None,
        session_id: str | None = None,
        due_before: datetime | None = None,
        limit: int = 100,
    ) -> tuple[dict[str, Any], ...]:
        if status is not None:
            ReminderScheduleStatus(status)
        if due_before is not None and (
            due_before.tzinfo is None or due_before.utcoffset() is None
        ):
            raise ValueError("reminder due boundary must be timezone-aware")
        _validate_limit(limit)
        with self._uow_factory() as uow:
            if session_id is not None:
                uow.catalog.get_session(session_id)
            return uow.reminders.list_schedules(
                status=status,
                session_id=session_id,
                due_before=_datetime(due_before) if due_before else None,
                limit=limit,
            )

    def due(
        self, at: datetime | None = None, limit: int = 100
    ) -> tuple[dict[str, Any], ...]:
        return self.list_schedules(due_before=at or self._now(), limit=limit)

    def schedule(self, event_id: str) -> dict[str, Any]:
        with self._uow_factory() as uow:
            schedule = uow.reminders.get_schedule(event_id)
            if schedule is None:
                raise KeyError(f"reminder does not exist: {event_id}")
            values = uow.reminders.list_schedules(
                status=None,
                session_id=schedule.session_id,
                due_before=None,
                limit=500,
            )
        return next(value for value in values if value["event_id"] == event_id)

    def feedback(self, candidate_id: str) -> tuple[dict[str, Any], ...]:
        with self._uow_factory() as uow:
            uow.reminders.get_candidate(candidate_id)
            return uow.reminders.list_feedback(candidate_id)

    def _event_proposals(
        self, intents: tuple[ReminderIntent, ...]
    ) -> tuple[tuple[ProposalKind, dict[str, Any], tuple[str, ...]], ...]:
        values: list[tuple[ProposalKind, dict[str, Any], tuple[str, ...]]] = []
        with self._uow_factory() as uow:
            for intent in intents:
                event_kind, operation = _event_operation(intent, uow)
                payload: dict[str, Any] = {
                    "operation": operation.value,
                    "session_id": intent.session_id,
                    "event_kind": event_kind.value,
                    "expected_revision": intent.expected_revision,
                    "patch": _event_patch(intent),
                }
                if intent.target_event_id is not None:
                    payload["event_id"] = intent.target_event_id
                values.append(
                    (
                        ProposalKind.EVENT_OPERATION,
                        payload,
                        intent.evidence_utterance_ids,
                    )
                )
        return tuple(values)

    def _classify(self, candidate_id: str, *, allow_auto_apply: bool) -> None:
        candidate = self._pending_candidate(candidate_id)
        with self._uow_factory() as uow:
            duplicate = (
                uow.reminders.duplicate_for(
                    candidate.dedup_key, exclude_candidate_id=candidate.candidate_id
                )
                if candidate.operation
                in {ReminderOperation.CREATE_TASK, ReminderOperation.CREATE_APPOINTMENT}
                else None
            )
            can_auto_apply = allow_auto_apply and self._auto_apply_allowed(
                uow, candidate
            )
        if duplicate is not None:
            self._reject_candidate(
                candidate,
                ReminderCandidateStatus.DUPLICATE,
                ReminderFeedbackAction.DEDUPLICATE,
                "reminder-policy",
                "duplicate_active_reminder",
                matched_event_id=duplicate["event_id"] or None,
                details={"duplicate_candidate_id": duplicate["candidate_id"]},
            )
            return
        if not can_auto_apply:
            return
        try:
            self._knowledge.accept_proposal(
                candidate.proposal_id,
                "reminder-policy",
                on_accepted=lambda uow, proposal, resolution, now: (
                    self._apply_candidate(
                        uow,
                        candidate,
                        resolution,
                        ReminderCandidateStatus.AUTO_APPLIED,
                        ReminderFeedbackAction.AUTO_APPLY,
                        "reminder-policy",
                        now,
                    )
                ),
            )
        except ValueError as exc:
            self._reject_candidate(
                candidate,
                ReminderCandidateStatus.CONFLICT,
                ReminderFeedbackAction.CONFLICT,
                "reminder-policy",
                str(exc),
            )

    def _auto_apply_allowed(
        self, uow: UnitOfWork, candidate: ReminderCandidate
    ) -> bool:
        if (
            candidate.operation
            not in {ReminderOperation.CREATE_TASK, ReminderOperation.CREATE_APPOINTMENT}
            or candidate.needs_confirmation
            or candidate.confidence < AUTO_APPLY_CONFIDENCE
            or candidate.actor_person_id != "self"
            or candidate.scheduled_at is None
            or candidate.scheduled_at <= self._now()
        ):
            return False
        proposal = uow.knowledge.get_proposal(candidate.proposal_id)
        facts = uow.knowledge.utterance_facts(proposal.evidence_utterance_ids)
        return (
            len(facts) == len(proposal.evidence_utterance_ids)
            and all(value["status"] == "active" for value in facts.values())
            and any(value["identity"] == "self" for value in facts.values())
        )

    def _pending_candidate(self, candidate_id: str) -> ReminderCandidate:
        with self._uow_factory() as uow:
            candidate = uow.reminders.get_candidate(candidate_id)
        if candidate.status is not ReminderCandidateStatus.PENDING_CONFIRMATION:
            raise ValueError("reminder candidate is no longer pending")
        return candidate

    def _reject_candidate(
        self,
        candidate: ReminderCandidate,
        status: ReminderCandidateStatus,
        action: ReminderFeedbackAction,
        actor: str,
        reason: str,
        *,
        matched_event_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if not reason.strip():
            raise ValueError("reminder rejection reason is required")

        def rejected(
            uow: UnitOfWork, proposal: StructuredProposal, now: datetime
        ) -> None:
            uow.reminders.resolve_candidate(
                candidate.candidate_id,
                status.value,
                matched_event_id,
                reason,
                _datetime(now),
                actor,
            )
            self._feedback(
                uow,
                candidate.candidate_id,
                action,
                actor,
                {"reason": reason, **(details or {})},
                now,
            )

        self._knowledge.reject_proposal(
            candidate.proposal_id,
            actor,
            reason,
            on_rejected=rejected,
        )
