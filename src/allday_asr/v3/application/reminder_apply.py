from __future__ import annotations

from datetime import datetime
from typing import Any

from allday_asr.v3.application.knowledge import ProposalResolution
from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.knowledge import (
    EventStatus,
)
from allday_asr.v3.domain.reminders import (
    CommitmentDirection,
    ReminderCandidate,
    ReminderCandidateStatus,
    ReminderFeedback,
    ReminderFeedbackAction,
    ReminderIntent,
    ReminderSchedule,
    ReminderScheduleStatus,
)
from allday_asr.v3.ports.repositories import UnitOfWork

from .reminder_support import (
    _datetime,
    _event_dedup_key,
    _parse_datetime,
    _schedule_projection,
)


class ReminderApplyMixin:
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
        ReminderApplyMixin._feedback(
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
