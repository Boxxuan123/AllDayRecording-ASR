from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from allday_asr.v3.domain.hashing import canonical_json_sha256
from allday_asr.v3.contracts import validate_reminder_dto
from allday_asr.v3.application.knowledge import (
    KnowledgeArchitectureService,
    ProposalResolution,
)
from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.knowledge import (
    EventKind,
    EventOperationKind,
    EventStatus,
    GenerationRecord,
    GenerationSubmission,
    KnowledgeLayer,
    ProposalKind,
    ProposalStatus,
    StructuredProposal,
)
from allday_asr.v3.domain.reminders import (
    CommitmentDirection,
    ReminderCandidate,
    ReminderCandidateStatus,
    ReminderFeedback,
    ReminderFeedbackAction,
    ReminderGenerationSubmission,
    ReminderIntent,
    ReminderOperation,
    ReminderSchedule,
    ReminderScheduleStatus,
)
from allday_asr.v3.ports.repositories import UnitOfWork


UnitOfWorkFactory = Callable[[], UnitOfWork]
DateTimeClock = Callable[[], datetime]

AUTO_APPLY_CONFIDENCE = 0.98


class IntelligentReminderService:
    """V3.3 reminder policy layered on V3.2 proposals and event operations."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        knowledge: KnowledgeArchitectureService,
        *,
        now: DateTimeClock | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._knowledge = knowledge
        self._now = now or _utc_now

    def submit_generation(
        self,
        submission: ReminderGenerationSubmission,
        *,
        allow_auto_apply: bool = True,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        _validate_generation(submission, allow_empty=allow_empty)
        if any(intent.operation is ReminderOperation.IGNORE for intent in submission.intents):
            raise ValueError("IGNORE is a review action, not a model event mutation")
        proposals = self._event_proposals(submission.intents)
        candidate_ids: list[str] = []

        def add_candidate(
            uow: UnitOfWork,
            generation: GenerationRecord,
            proposal: StructuredProposal,
            index: int,
            created_at: datetime,
        ) -> None:
            intent = submission.intents[index]
            candidate = ReminderCandidate(
                candidate_id=new_ulid(),
                proposal_id=proposal.proposal_id,
                generation_id=generation.generation_id,
                operation=intent.operation,
                session_id=intent.session_id,
                title=intent.title.strip() if intent.title else None,
                actor_person_id=intent.actor_person_id.strip(),
                commitment_direction=intent.commitment_direction,
                related_person_ids=intent.related_person_ids,
                scheduled_at=intent.scheduled_at,
                location=intent.location.strip() if intent.location else None,
                confidence=intent.confidence,
                needs_confirmation=intent.needs_confirmation,
                target_event_id=intent.target_event_id,
                expected_revision=intent.expected_revision,
                dedup_key=_candidate_dedup_key(intent),
                status=ReminderCandidateStatus.PENDING_CONFIRMATION,
                created_at=created_at,
            )
            if not uow.reminders.add_candidate(candidate):
                raise RuntimeError("reminder candidate identity collision")
            candidate_ids.append(candidate.candidate_id)

        result = self._knowledge.submit_generation(
            GenerationSubmission(
                layer=KnowledgeLayer.EVENT,
                producer=submission.producer,
                producer_version=submission.producer_version,
                model=submission.model,
                prompt_version=submission.prompt_version,
                extractor_version=submission.extractor_version,
                input_scope=dict(submission.input_scope),
                proposals=proposals,
            ),
            on_proposal_created=add_candidate,
            allow_empty=allow_empty,
        )
        for candidate_id in candidate_ids:
            self._classify(candidate_id, allow_auto_apply=allow_auto_apply)
        result.pop("proposals", None)
        result["candidates"] = [self.candidate(candidate_id) for candidate_id in candidate_ids]
        return result

    def confirm(self, candidate_id: str, actor: str) -> dict[str, Any]:
        candidate = self._pending_candidate(candidate_id)
        try:
            resolution = self._knowledge.accept_proposal(
                candidate.proposal_id,
                actor,
                on_accepted=lambda uow, proposal, value, now: self._apply_candidate(
                    uow,
                    candidate,
                    value,
                    ReminderCandidateStatus.CONFIRMED,
                    ReminderFeedbackAction.CONFIRM,
                    actor,
                    now,
                ),
            )
        except ValueError as exc:
            self._reject_candidate(
                candidate,
                ReminderCandidateStatus.CONFLICT,
                ReminderFeedbackAction.CONFLICT,
                actor,
                str(exc),
            )
            return self.candidate(candidate_id)
        return {
            "candidate": self.candidate(candidate_id),
            "resolution": resolution.as_dict(),
            "reminder": self.schedule(str(resolution.resource_id)),
        }

    def modify(
        self, candidate_id: str, actor: str, changes: dict[str, Any]
    ) -> dict[str, Any]:
        original = self._pending_candidate(candidate_id)
        allowed = {"title", "scheduled_at", "location"}
        if not changes or set(changes) - allowed:
            raise ValueError("reminder modification fields are invalid")
        title = changes.get("title", original.title)
        location = changes.get("location", original.location)
        scheduled_at = (
            _parse_datetime(changes["scheduled_at"])
            if "scheduled_at" in changes
            else original.scheduled_at
        )
        with self._uow_factory() as uow:
            original_evidence = uow.knowledge.get_proposal(
                original.proposal_id
            ).evidence_utterance_ids
        revised = replace(
            self._intent_from_candidate(original, original_evidence),
            title=title,
            location=location,
            scheduled_at=scheduled_at,
            needs_confirmation=True,
        )
        result = self.submit_generation(
            ReminderGenerationSubmission(
                producer="desktop-user",
                producer_version="3.3.0",
                model="manual-edit",
                prompt_version="manual-edit.1",
                extractor_version="manual-edit.1",
                input_scope={
                    "session_id": original.session_id,
                    "revises_candidate_id": candidate_id,
                },
                intents=(revised,),
            ),
            allow_auto_apply=False,
        )
        replacement_id = str(result["candidates"][0]["candidate_id"])
        replacement = self._pending_candidate(replacement_id)

        def finalize_edit(
            uow: UnitOfWork,
            proposal: StructuredProposal,
            resolution: ProposalResolution,
            now: datetime,
        ) -> None:
            self._apply_candidate(
                uow,
                replacement,
                resolution,
                ReminderCandidateStatus.CONFIRMED,
                ReminderFeedbackAction.CONFIRM,
                actor,
                now,
            )
            current_original = uow.knowledge.get_proposal(original.proposal_id)
            if current_original.status is not ProposalStatus.PENDING:
                raise ValueError("original reminder candidate is no longer pending")
            uow.knowledge.resolve_proposal(
                original.proposal_id,
                ProposalStatus.REJECTED.value,
                _datetime(now),
                actor,
                "superseded_by_user_edit",
            )
            uow.reminders.resolve_candidate(
                original.candidate_id,
                ReminderCandidateStatus.MODIFIED.value,
                str(resolution.resource_id),
                None,
                _datetime(now),
                actor,
            )
            self._feedback(
                uow,
                original.candidate_id,
                ReminderFeedbackAction.MODIFY,
                actor,
                {"replacement_candidate_id": replacement.candidate_id, **changes},
                now,
            )

        resolution = self._knowledge.accept_proposal(
            replacement.proposal_id,
            actor,
            on_accepted=finalize_edit,
        )
        return {
            "candidate": self.candidate(candidate_id),
            "replacement": self.candidate(replacement_id),
            "resolution": resolution.as_dict(),
            "reminder": self.schedule(str(resolution.resource_id)),
        }

    def ignore(self, candidate_id: str, actor: str, reason: str) -> dict[str, Any]:
        candidate = self._pending_candidate(candidate_id)
        self._reject_candidate(
            candidate,
            ReminderCandidateStatus.IGNORED,
            ReminderFeedbackAction.IGNORE,
            actor,
            reason,
        )
        return self.candidate(candidate_id)

    def deliver(self, event_id: str, actor: str = "local-scheduler") -> dict[str, Any]:
        now = self._now()
        with self._uow_factory() as uow:
            schedule = uow.reminders.get_schedule(event_id)
            if schedule is None:
                raise KeyError(f"reminder does not exist: {event_id}")
            uow.reminders.mark_delivered(event_id, _datetime(now))
            self._feedback(
                uow,
                schedule.source_candidate_id,
                ReminderFeedbackAction.DELIVER,
                actor,
                {"event_id": event_id, "event_revision": schedule.event_revision},
                now,
            )
            uow.audit.append(
                "reminder.delivered",
                actor,
                "event",
                event_id,
                {"event_revision": schedule.event_revision},
            )
        return self.schedule(event_id)

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

    def due(self, at: datetime | None = None, limit: int = 100) -> tuple[dict[str, Any], ...]:
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
                on_accepted=lambda uow, proposal, resolution, now: self._apply_candidate(
                    uow,
                    candidate,
                    resolution,
                    ReminderCandidateStatus.AUTO_APPLIED,
                    ReminderFeedbackAction.AUTO_APPLY,
                    "reminder-policy",
                    now,
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

    @staticmethod
    def _apply_candidate(
        uow: UnitOfWork,
        candidate: ReminderCandidate,
        resolution: ProposalResolution,
        candidate_status: ReminderCandidateStatus,
        action: ReminderFeedbackAction,
        actor: str,
        now: datetime,
    ) -> None:
        if resolution.resource_type != "event" or resolution.resource_id is None:
            raise ValueError("reminder proposal did not resolve to an event")
        state = uow.knowledge.get_event(resolution.resource_id)
        if state is None:
            raise ValueError("accepted reminder event is missing")
        previous = uow.reminders.get_schedule(state.event_id)
        title = state.payload.get("title")
        scheduled_value = state.payload.get("scheduled_time")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("accepted reminder event has no title")
        if not isinstance(scheduled_value, str):
            raise ValueError("accepted reminder event has no scheduled time")
        scheduled_at = _parse_datetime(scheduled_value)
        if state.status is EventStatus.CANCELLED:
            schedule_status = ReminderScheduleStatus.CANCELLED
        elif state.status is EventStatus.COMPLETED:
            schedule_status = ReminderScheduleStatus.COMPLETED
        elif (
            previous is not None
            and previous.status is ReminderScheduleStatus.DELIVERED
            and previous.scheduled_at == scheduled_at
        ):
            schedule_status = ReminderScheduleStatus.DELIVERED
        else:
            schedule_status = ReminderScheduleStatus.SCHEDULED
        direction = CommitmentDirection(
            str(state.payload.get("commitment_direction", "not_applicable"))
        )
        related = state.payload.get("related_person_ids", [])
        if not isinstance(related, list) or not all(
            isinstance(value, str) for value in related
        ):
            raise ValueError("accepted reminder related people are invalid")
        actor_person_id = state.payload.get("actor_person_id")
        if not isinstance(actor_person_id, str) or not actor_person_id:
            raise ValueError("accepted reminder event has no actor")
        schedule = ReminderSchedule(
            event_id=state.event_id,
            session_id=state.session_id,
            event_revision=state.revision,
            source_candidate_id=candidate.candidate_id,
            title=title.strip(),
            actor_person_id=actor_person_id,
            commitment_direction=direction,
            related_person_ids=tuple(related),
            scheduled_at=scheduled_at,
            location=(
                str(state.payload["location"])
                if state.payload.get("location") is not None
                else None
            ),
            status=schedule_status,
            dedup_key=_event_dedup_key(state.payload),
            delivered_at=(
                previous.delivered_at
                if previous is not None
                and schedule_status is ReminderScheduleStatus.DELIVERED
                else None
            ),
            created_at=previous.created_at if previous is not None else now,
            updated_at=now,
        )
        uow.reminders.put_schedule(schedule)
        uow.changes.append(
            "reminder",
            schedule.event_id,
            schedule.event_revision,
            "upsert",
            _schedule_projection(schedule),
        )
        uow.reminders.resolve_candidate(
            candidate.candidate_id,
            candidate_status.value,
            state.event_id,
            None,
            _datetime(now),
            actor,
        )
        IntelligentReminderService._feedback(
            uow,
            candidate.candidate_id,
            action,
            actor,
            {
                "event_id": state.event_id,
                "event_revision": state.revision,
                "schedule_status": schedule.status.value,
            },
            now,
        )
        uow.audit.append(
            "reminder.candidate.applied",
            actor,
            "reminder_candidate",
            candidate.candidate_id,
            {
                "event_id": state.event_id,
                "event_revision": state.revision,
                "candidate_status": candidate_status.value,
            },
        )

    @staticmethod
    def _feedback(
        uow: UnitOfWork,
        candidate_id: str,
        action: ReminderFeedbackAction,
        actor: str,
        details: dict[str, Any],
        now: datetime,
    ) -> None:
        if not uow.reminders.add_feedback(
            ReminderFeedback(
                feedback_id=new_ulid(),
                candidate_id=candidate_id,
                action=action,
                actor=actor,
                details=details,
                created_at=now,
            )
        ):
            raise RuntimeError("reminder feedback identity collision")

    @staticmethod
    def _intent_from_candidate(
        candidate: ReminderCandidate, evidence_utterance_ids: tuple[str, ...]
    ) -> ReminderIntent:
        return ReminderIntent(
            operation=candidate.operation,
            session_id=candidate.session_id,
            title=candidate.title,
            actor_person_id=candidate.actor_person_id,
            commitment_direction=candidate.commitment_direction,
            related_person_ids=candidate.related_person_ids,
            scheduled_at=candidate.scheduled_at,
            location=candidate.location,
            confidence=candidate.confidence,
            evidence_utterance_ids=evidence_utterance_ids,
            needs_confirmation=candidate.needs_confirmation,
            target_event_id=candidate.target_event_id,
            expected_revision=candidate.expected_revision,
        )


def _event_operation(
    intent: ReminderIntent, uow: UnitOfWork
) -> tuple[EventKind, EventOperationKind]:
    if intent.operation is ReminderOperation.CREATE_TASK:
        return EventKind.TASK, EventOperationKind.CREATE
    if intent.operation is ReminderOperation.CREATE_APPOINTMENT:
        return EventKind.APPOINTMENT, EventOperationKind.CREATE
    if intent.target_event_id is None:
        raise ValueError("reminder mutation target is required")
    current = uow.knowledge.get_event(intent.target_event_id)
    if current is None:
        raise ValueError("reminder target event does not exist")
    if current.session_id != intent.session_id:
        raise ValueError("reminder target belongs to another session")
    if current.revision != intent.expected_revision:
        raise ValueError("reminder target revision conflict")
    if current.event_kind not in {EventKind.TASK, EventKind.APPOINTMENT}:
        raise ValueError("only task and appointment events can become reminders")
    mapping = {
        ReminderOperation.UPDATE_EVENT: EventOperationKind.UPDATE,
        ReminderOperation.CANCEL_EVENT: EventOperationKind.CANCEL,
        ReminderOperation.MARK_DONE: EventOperationKind.COMPLETE,
    }
    return current.event_kind, mapping[intent.operation]


def _event_patch(intent: ReminderIntent) -> dict[str, Any]:
    if intent.operation in {
        ReminderOperation.CANCEL_EVENT,
        ReminderOperation.MARK_DONE,
    }:
        return {
            "last_reminder_signal": {
                "operation": intent.operation.value,
                "confidence": intent.confidence,
                "reason": intent.reason,
            }
        }
    patch: dict[str, Any] = {
        "actor_person_id": intent.actor_person_id,
        "commitment_direction": intent.commitment_direction.value,
        "related_person_ids": list(intent.related_person_ids),
        "confidence": intent.confidence,
        "needs_confirmation": intent.needs_confirmation,
    }
    if intent.title is not None:
        patch["title"] = intent.title.strip()
    if intent.scheduled_at is not None:
        patch["scheduled_time"] = _datetime(intent.scheduled_at)
    if intent.location is not None:
        patch["location"] = intent.location.strip()
    return patch


def _candidate_dedup_key(intent: ReminderIntent) -> str:
    if intent.operation in {
        ReminderOperation.CREATE_TASK,
        ReminderOperation.CREATE_APPOINTMENT,
    }:
        return _event_dedup_key(_event_patch(intent))
    return canonical_json_sha256(
        {
            "operation": intent.operation.value,
            "target_event_id": intent.target_event_id,
            "expected_revision": intent.expected_revision,
            "patch": _event_patch(intent),
        }
    )


def _event_dedup_key(payload: dict[str, Any]) -> str:
    title = str(payload.get("title", ""))
    return canonical_json_sha256(
        {
            "title": " ".join(title.casefold().split()),
            "actor_person_id": payload.get("actor_person_id"),
            "commitment_direction": payload.get("commitment_direction"),
            "related_person_ids": sorted(payload.get("related_person_ids", [])),
            "scheduled_time": payload.get("scheduled_time"),
            "location": (
                " ".join(str(payload["location"]).casefold().split())
                if payload.get("location")
                else None
            ),
        }
    )


def _schedule_projection(schedule: ReminderSchedule) -> dict[str, Any]:
    return validate_reminder_dto({
        "event_id": schedule.event_id,
        "session_id": schedule.session_id,
        "event_revision": schedule.event_revision,
        "source_candidate_id": schedule.source_candidate_id,
        "title": schedule.title,
        "actor_person_id": schedule.actor_person_id,
        "commitment_direction": schedule.commitment_direction.value,
        "related_person_ids": list(schedule.related_person_ids),
        "scheduled_at": _datetime(schedule.scheduled_at),
        "location": schedule.location,
        "status": schedule.status.value,
        "updated_at": _datetime(schedule.updated_at),
    })


def _validate_generation(
    submission: ReminderGenerationSubmission, *, allow_empty: bool = False
) -> None:
    for name, value in (
        ("producer", submission.producer),
        ("producer_version", submission.producer_version),
        ("model", submission.model),
        ("prompt_version", submission.prompt_version),
        ("extractor_version", submission.extractor_version),
    ):
        if not value.strip():
            raise ValueError(f"reminder generation {name} is required")
    if not submission.input_scope or not isinstance(submission.input_scope, dict):
        raise ValueError("reminder generation input scope is required")
    if not submission.intents and not allow_empty:
        raise ValueError("reminder generation requires at least one intent")


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= 500:
        raise ValueError("reminder page limit must be between 1 and 500")


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("reminder scheduled_at must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("reminder scheduled_at must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("reminder scheduled_at must include a timezone")
    return parsed


def _datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("reminder timestamp must be timezone-aware")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = ["AUTO_APPLY_CONFIDENCE", "IntelligentReminderService"]
