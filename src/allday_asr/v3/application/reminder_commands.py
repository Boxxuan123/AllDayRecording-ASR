from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

from allday_asr.v3.application.knowledge import ProposalResolution
from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.knowledge import (
    GenerationRecord,
    GenerationSubmission,
    KnowledgeLayer,
    ProposalStatus,
    StructuredProposal,
)
from allday_asr.v3.domain.reminders import (
    ReminderCandidate,
    ReminderCandidateStatus,
    ReminderFeedbackAction,
    ReminderGenerationSubmission,
    ReminderOperation,
)
from allday_asr.v3.ports.repositories import UnitOfWork

from .reminder_support import (
    _candidate_dedup_key,
    _datetime,
    _parse_datetime,
    _validate_generation,
)


class ReminderCommandMixin:
    def submit_generation(
        self,
        submission: ReminderGenerationSubmission,
        *,
        allow_auto_apply: bool = True,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        _validate_generation(submission, allow_empty=allow_empty)
        if any(
            intent.operation is ReminderOperation.IGNORE
            for intent in submission.intents
        ):
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
        result["candidates"] = [
            self.candidate(candidate_id) for candidate_id in candidate_ids
        ]
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
