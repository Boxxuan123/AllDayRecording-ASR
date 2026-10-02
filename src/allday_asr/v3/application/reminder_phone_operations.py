"""Reminder commands in the existing sync transaction and immutable receipts."""
from dataclasses import replace
from datetime import datetime

from allday_asr.v3.domain.device_sync import ClientOperationStatus, OperationReceipt
from allday_asr.v3.domain.hashing import canonical_json_sha256
from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.knowledge import (
    GenerationRecord, GenerationStatus, KnowledgeLayer, ProposalKind,
    ProposalStatus, StructuredProposal,
)
from allday_asr.v3.domain.reminders import (
    ReminderCandidateStatus, ReminderFeedbackAction, ReminderOperation,
)
from .knowledge import KnowledgeArchitectureService
from .reminder_apply import ReminderApplyMixin
from .reminder_support import _datetime, _parse_datetime, _candidate_dedup_key


def apply_reminder_operation(operation, uow, now: datetime):
    actor = f"phone-operation:{operation.operation_id}"
    # All expected validation failures happen before the first write. Any
    # unexpected persistence error propagates, rolling back the sync transaction.
    try:
        candidate, proposal, changes = _validate(operation, uow, now)
    except (ValueError, KeyError, TypeError) as exc:
        return OperationReceipt(operation.operation_id, ClientOperationStatus.CONFLICT,
            None, {"code": "REMINDER_CONFLICT", "message": str(exc)})
    action = operation.payload["action"]
    if action == "ignore":
        uow.knowledge.resolve_proposal(proposal.proposal_id, "rejected", _datetime(now), actor, "phone_ignored")
        uow.reminders.resolve_candidate(candidate.candidate_id, "ignored", None, "phone_ignored", _datetime(now), actor)
        ReminderApplyMixin._feedback(uow, candidate.candidate_id, ReminderFeedbackAction.IGNORE, actor, {}, now)
        return OperationReceipt(operation.operation_id, ClientOperationStatus.APPLIED, 1, None)
    original = candidate
    if changes or operation.kind == "reminder.task":
        candidate, proposal = _manual_proposal(operation, uow, candidate, proposal, changes, now)
    knowledge = KnowledgeArchitectureService(lambda: None)
    resolution = knowledge._accept_event(uow, proposal, actor, now)
    uow.knowledge.resolve_proposal(proposal.proposal_id, "accepted", _datetime(now), actor, None)
    ReminderApplyMixin._apply_candidate(uow, candidate, resolution,
        ReminderCandidateStatus.CONFIRMED, ReminderFeedbackAction.CONFIRM, actor, now)
    if operation.kind == "reminder.review" and changes:
        uow.knowledge.resolve_proposal(original.proposal_id, "rejected", _datetime(now), actor, "superseded_by_user_edit")
        uow.reminders.resolve_candidate(original.candidate_id, "modified", resolution.resource_id, None, _datetime(now), actor)
        ReminderApplyMixin._feedback(uow, original.candidate_id, ReminderFeedbackAction.MODIFY,
            actor, {"replacement_candidate_id": candidate.candidate_id, **changes}, now)
    return OperationReceipt(operation.operation_id, ClientOperationStatus.APPLIED, resolution.resource_revision, None,
        ({"resource_id": resolution.resource_id, "revision": resolution.resource_revision},))


def _validate(operation, uow, now):
    payload = operation.payload
    action = payload.get("action")
    if operation.kind == "reminder.review":
        if set(payload) - {"candidate_id", "action", "title", "scheduled_at"}:
            raise ValueError("invalid reminder review fields")
        if action not in {"confirm", "edit", "ignore"}:
            raise ValueError("invalid reminder review action")
        candidate = uow.reminders.get_candidate(payload["candidate_id"])
        if candidate.status is not ReminderCandidateStatus.PENDING_CONFIRMATION:
            raise ValueError("candidate no longer pending; refresh before deciding")
        proposal = uow.knowledge.get_proposal(candidate.proposal_id)
        changes = {key: payload[key] for key in ("title", "scheduled_at") if key in payload}
        if action != "edit" and changes:
            raise ValueError("only edit can change candidate fields")
        if action == "edit" and not changes:
            raise ValueError("candidate edit is empty")
        if action == "ignore":
            return candidate, proposal, changes
    elif operation.kind == "reminder.task":
        if set(payload) - {"event_id", "action", "title", "scheduled_at"}:
            raise ValueError("invalid task fields")
        if action not in {"reschedule", "cancel", "complete"}:
            raise ValueError("invalid task action")
        schedule = uow.reminders.get_schedule(payload["event_id"])
        if schedule is None or schedule.status.value not in {"scheduled", "delivered"}:
            raise ValueError("task is no longer active")
        if operation.base_revision != schedule.event_revision:
            raise ValueError("task revision changed; refresh before deciding")
        candidate = uow.reminders.get_candidate(schedule.source_candidate_id)
        proposal = uow.knowledge.get_proposal(candidate.proposal_id)
        changes = {key: payload[key] for key in ("title", "scheduled_at") if key in payload}
        if action != "reschedule" and changes:
            raise ValueError("terminal task action cannot change fields")
        if action == "reschedule" and "scheduled_at" not in changes:
            raise ValueError("reschedule requires a future due time")
        state = uow.knowledge.get_event(schedule.event_id)
        if state is None or state.derivation_status != "active":
            raise ValueError("task evidence is stale")
        if state.revision != schedule.event_revision or state.status.value != "active":
            raise ValueError("task event changed; refresh before deciding")
    else:
        raise ValueError("unknown reminder command")
    if candidate.actor_person_id != "self":
        raise ValueError("only explicit self tasks can be confirmed")
    if "title" in changes and (not isinstance(changes["title"], str) or not changes["title"].strip()):
        raise ValueError("task title is empty")
    if "scheduled_at" in changes:
        if not isinstance(changes["scheduled_at"], str):
            raise ValueError("due time must be an absolute timestamp")
        if _parse_datetime(changes["scheduled_at"]) <= now:
            raise ValueError("due time must be in the future")
    if operation.kind == "reminder.review":
        if candidate.operation is not ReminderOperation.CREATE_TASK:
            raise ValueError("this release confirms only one-off self tasks")
        if proposal.status is not ProposalStatus.PENDING:
            raise ValueError("proposal no longer pending")
        generation = uow.knowledge.get_generation(candidate.generation_id)
        if generation.status is not GenerationStatus.SUCCEEDED:
            raise ValueError("candidate generation is stale")
        facts = uow.knowledge.utterance_facts(proposal.evidence_utterance_ids)
        inputs = generation.input_scope.get("resolved_inputs", {}).get("utterances", [])
        revisions = uow.knowledge.utterance_revisions(proposal.evidence_utterance_ids)
        if (not facts or any(value["status"] != "active" or value["identity"] != "self" for value in facts.values())
            or any(revisions.get(item["utterance_id"], (None, None))[1] != item["revision"] for item in inputs)):
            raise ValueError("source identity or revision changed")
        due = _parse_datetime(changes["scheduled_at"]) if "scheduled_at" in changes else candidate.scheduled_at
        if due is None or due <= now:
            raise ValueError("due time must be in the future")
    return candidate, proposal, changes


def _manual_proposal(operation, uow, original, source_proposal, changes, now):
    intent = ReminderApplyMixin._intent_from_candidate(original, source_proposal.evidence_utterance_ids)
    if operation.kind == "reminder.task":
        schedule = uow.reminders.get_schedule(operation.payload["event_id"])
        intent = replace(intent, target_event_id=schedule.event_id, expected_revision=schedule.event_revision,
            title=schedule.title, scheduled_at=schedule.scheduled_at,
            operation={"reschedule": ReminderOperation.UPDATE_EVENT,
                "cancel": ReminderOperation.CANCEL_EVENT, "complete": ReminderOperation.MARK_DONE}[operation.payload["action"]])
    intent = replace(intent, title=changes.get("title", intent.title),
        scheduled_at=_parse_datetime(changes["scheduled_at"]) if "scheduled_at" in changes else intent.scheduled_at,
        needs_confirmation=True)
    from .reminder_support import _event_operation, _event_patch
    kind, event_operation = _event_operation(intent, uow)
    payload = {"session_id": intent.session_id, "event_kind": kind.value,
        "operation": event_operation.value, "expected_revision": intent.expected_revision, "patch": _event_patch(intent)}
    if intent.target_event_id:
        payload["event_id"] = intent.target_event_id
    digest = canonical_json_sha256({"operation_id": operation.operation_id})
    generation = GenerationRecord(new_ulid(), KnowledgeLayer.EVENT, "phone-user", "3.3.0",
        "manual-edit", "phone-command.1", "phone-command.1", {"operation_id": operation.operation_id},
        digest, 1, GenerationStatus.COLLECTING, now)
    uow.knowledge.add_generation(generation)
    proposal = StructuredProposal(new_ulid(), generation.generation_id, ProposalKind.EVENT_OPERATION,
        payload, source_proposal.evidence_utterance_ids, ProposalStatus.PENDING, now)
    uow.knowledge.add_proposal(proposal)
    uow.knowledge.complete_generation(generation.generation_id, "succeeded", _datetime(now), None)
    candidate = replace(original, candidate_id=new_ulid(), proposal_id=proposal.proposal_id,
        generation_id=generation.generation_id, operation=intent.operation, title=intent.title,
        scheduled_at=intent.scheduled_at, target_event_id=intent.target_event_id, expected_revision=intent.expected_revision,
        dedup_key=_candidate_dedup_key(intent), status=ReminderCandidateStatus.PENDING_CONFIRMATION,
        matched_event_id=None, conflict_reason=None, created_at=now, resolved_at=None, resolved_by=None)
    uow.reminders.add_candidate(candidate)
    return candidate, proposal
