"""Chat evidence enriches the existing Event aggregate and manual history."""

from datetime import datetime, timezone
from dataclasses import replace
from allday_asr.v3.domain.reminders import ReminderScheduleStatus
from .reminder_support import _schedule_projection
from hashlib import sha256

from allday_asr.v3.domain.chat_data import packed
from allday_asr.v3.domain.chat_projection import semantic_value
from allday_asr.v3.domain.chat_admission import POLICY, decide, evidence_digest
from .chat_followup_admission import ChatRelevanceAdmission
from .chat_followup_commands import ChatFollowupCommands
from allday_asr.v3.domain.chat_followups import VERSION, validate_candidate
from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.knowledge import (
    EventCurrentState,
    EventKind,
    EventOperation,
    EventOperationKind,
    EventStatus,
    GenerationRecord,
    GenerationStatus,
    KnowledgeLayer,
    ProposalKind,
    ProposalStatus,
    StructuredProposal,
)
from allday_asr.v3.ports.chat_data import ChatDataError


class ChatFollowups(ChatFollowupCommands, ChatRelevanceAdmission):
    def __init__(self, uow_factory, *, now=None, reminders=None, identity_cache=None):
        self.uow_factory = uow_factory
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.reminders = reminders
        self.identity_cache = identity_cache

    def list(self, limit=15, offset=0):
        if not 1 <= limit <= 100 or offset < 0:
            raise ChatDataError("INVALID_LIMIT", 400)
        with self.uow_factory().reading() as uow:
            rows = uow.followups.items(limit + 1, offset)
            return {
                "items": [self._view(r) for r in rows[:limit]],
                "has_more": len(rows) > limit,
                "offset": offset,
            }

    def _view(self, row):
        row = semantic_value(row)
        payload = row["payload"]
        chat = payload.get("chat_followup", {})
        if self.identity_cache is not None:
            from .chat_followup_identity import hydrate_identity

            hydrate_identity(chat, self.identity_cache)
        category = (
            "ended"
            if row["status"] != "active" or row["ignored"]
            else chat.get("category", "pending")
        )
        if category in {"self", "waiting", "pending"} and chat.get("historical"):
            category = "pending"
        return {**row, "category": category, "chat": chat}

    def apply(
        self, candidate, records, dataset, links, *, timezone_name=None, provenance=None
    ):
        value = validate_candidate(
            candidate,
            records,
            dataset,
            links,
            int(self.now().timestamp()),
            timezone_name,
        )
        value["provenance"] = provenance or {
            "model": "not_recorded",
            "source": "local_application",
        }
        key = value["source_key"]
        effect = sha256(
            packed(
                [
                    VERSION,
                    POLICY,
                    value["interpretation_key"],
                    value["action"],
                    value.get("target_event_id"),
                    [
                        value["source_key"],
                        *sorted(r["evidence_key"] for r in value["evidence"]),
                    ],
                ]
            ).encode()
        ).hexdigest()
        with self.uow_factory() as uow:
            source = uow.followups.source(key)
            if source and (
                source["dataset"] != dataset
                or source["conversation_key"] != value["conversation_key"]
            ):
                return {"effect": "source_changed", "source_key": key}
            if uow.followups.effect_exists(effect):
                return {"effect": "duplicate", "source_key": key}
            admission = uow.followups.admission(key, dataset)
            if (
                admission
                and admission["origin"] == "human_selection"
                and admission["evidence_digest"] != evidence_digest(value)
                and value.get("target_event_id")
                and (not source or source["event_id"] != value["target_event_id"])
            ):
                raise ChatDataError("UNVERIFIED_FOLLOWUP_TARGET", 422)
            decision, reason = decide(value, links, admission)
            if decision in {"text_only", "conditional"}:
                if admission is None:
                    uow.followups.save_admission(
                        self._admission(value, decision, "rule", POLICY)
                    )
                if source and reason == "review_evidence_changed":
                    value["change_kind"] = "evidence_replaced"
                    source["conflict_json"] = packed(value)
                    uow.followups.save_source(key, source)
                    self._append(
                        uow,
                        uow.knowledge.get_event(source["event_id"]),
                        {},
                        EventOperationKind.UPDATE,
                        "system:chat-conflict",
                    )
                    uow.followups.effect(
                        effect, source["event_id"], {"result": "evidence_conflict"}
                    )
                    return {
                        "effect": "evidence_conflict",
                        "event_id": source["event_id"],
                        "source_key": key,
                    }
                return self._exclude(uow, source, value, reason)
            value["personal_scope"] = True
            value["admission"] = {
                "decision": decision,
                "reason": reason,
                "policy": POLICY,
            }
            if decision == "cared":
                value["category"] = "cared"
            target = value.get("target_event_id")
            sources = uow.followups.for_event(target) if target else []
            existing = next(
                (
                    s
                    for s in sources
                    if s["event_id"] == target
                    and s["conversation_key"] == value["conversation_key"]
                    and s["dataset"] == dataset
                ),
                None,
            )
            if target and existing is None:
                raise ChatDataError("UNVERIFIED_FOLLOWUP_TARGET", 422)
            if (
                value["action"] != "create"
                and existing
                and value["category"] != "pending"
            ):
                source = existing
            elif value["action"] != "create":
                value["proposed_target"] = target
                value["action"] = "create"
                value["category"] = "pending"
            if uow.followups.effect_exists(effect):
                return {"effect": "duplicate", "source_key": key}
            current = uow.knowledge.get_event(source["event_id"]) if source else None
            if (
                source
                and value["latest_sent_at"] is not None
                and source["latest_sent_at"] is not None
                and (
                    value["latest_sent_at"] < source["latest_sent_at"]
                    or (
                        value["latest_sent_at"] == source["latest_sent_at"]
                        and (
                            value["action"] != "create"
                            or source["human_override"]
                            or source["ignored"]
                        )
                    )
                )
            ):
                uow.followups.effect(
                    effect,
                    source["event_id"],
                    {"result": "older_or_same_evidence", "version": VERSION},
                )
                return {
                    "effect": "older_or_same_evidence",
                    "event_id": source["event_id"],
                }
            if source and (
                source["human_override"]
                or source["ignored"]
                or uow.reminders.is_user_confirmed_task(current.event_id)
            ):
                value["change_kind"] = "progress"
                source["conflict_json"] = packed(value)
                uow.followups.save_source(source["source_key"], source)
                self._append(
                    uow, current, {}, EventOperationKind.UPDATE, "system:chat-conflict"
                )
                uow.followups.effect(
                    effect,
                    source["event_id"],
                    {"result": "human_conflict", "evidence": value["evidence"]},
                )
                return {"effect": "human_conflict", "event_id": source["event_id"]}
            if current and current.status is not EventStatus.ACTIVE:
                value["change_kind"] = "progress"
                source["conflict_json"] = packed(value)
                uow.followups.save_source(source["source_key"], source)
                self._append(
                    uow, current, {}, EventOperationKind.UPDATE, "system:chat-conflict"
                )
                uow.followups.effect(
                    effect, current.event_id, {"result": "ended_conflict"}
                )
                return {"effect": "ended_conflict", "event_id": current.event_id}
            old_chat = current.payload.get("chat_followup", {}) if current else {}
            evidence = {r["evidence_key"]: r for r in old_chat.get("evidence", [])}
            evidence.update({r["evidence_key"]: r for r in value["evidence"]})
            value["evidence"] = list(evidence.values())
            patch = {"title": value["title"], "chat_followup": value}
            kind = EventOperationKind(
                "update"
                if current and value["action"] == "create"
                else (value["action"] if current else "create")
            )
            state = self._append(uow, current, patch, kind, "system:chat-followup")
            if source is None:
                source = {
                    "event_id": state.event_id,
                    "dataset": dataset,
                    "conversation_key": value["conversation_key"],
                }
            source["latest_sent_at"] = value["latest_sent_at"]
            uow.followups.save_source(source.get("source_key", key), source)
            # Publish accepted rule evidence only after the event update succeeds.
            # A previous partial rule exclusion must not be replayed over newer proof.
            if admission is None or admission["origin"] == "rule":
                uow.followups.save_admission(
                    self._admission(value, decision, "rule", POLICY)
                )
            uow.followups.effect(
                effect,
                state.event_id,
                {
                    "result": "applied",
                    "revision": state.revision,
                    "evidence": value["evidence"],
                },
            )
            return {
                "effect": "applied",
                "event_id": state.event_id,
                "revision": state.revision,
            }

    def _append(self, uow, current, patch, kind, actor):
        patch = semantic_value(patch)
        now = self.now()
        event_id = current.event_id if current else new_ulid()
        revision = current.revision + 1 if current else 1
        generation_id, proposal_id, operation_id = new_ulid(), new_ulid(), new_ulid()
        body = {
            "event_id": event_id,
            "patch": patch,
            "operation": kind.value,
            "expected_revision": revision - 1,
            "session_id": current.session_id if current else None,
            "event_kind": current.event_kind.value
            if current
            else (
                "important_experience"
                if patch.get("chat_followup", {}).get("category") == "cared"
                else "task"
            ),
        }
        digest = sha256(packed(body).encode()).hexdigest()
        generation = GenerationRecord(
            generation_id,
            KnowledgeLayer.EVENT,
            "chat-followup",
            VERSION,
            "manual"
            if actor.startswith(("desktop-user", "phone-user"))
            else patch.get("chat_followup", {})
            .get("provenance", {})
            .get("model", "not_recorded"),
            VERSION,
            VERSION,
            {"chat_source": True, "evidence_type": "chat_record", "version": VERSION},
            digest,
            1,
            GenerationStatus.COLLECTING,
            now,
        )
        uow.knowledge.add_generation(generation)
        proposal = StructuredProposal(
            proposal_id,
            generation_id,
            ProposalKind.EVENT_OPERATION,
            body,
            (),
            ProposalStatus.PENDING,
            now,
        )
        uow.knowledge.add_proposal(proposal)
        uow.knowledge.complete_generation(
            generation_id, "succeeded", now.isoformat(), None
        )
        uow.knowledge.resolve_proposal(
            proposal_id, "accepted", now.isoformat(), actor, None
        )
        status = current.status if current else EventStatus.ACTIVE
        if kind is EventOperationKind.COMPLETE:
            status = EventStatus.COMPLETED
        elif kind is EventOperationKind.CANCEL:
            status = EventStatus.CANCELLED
        elif kind is EventOperationKind.REOPEN:
            status = EventStatus.ACTIVE
        payload = {**(current.payload if current else {}), **patch}
        operation = EventOperation(
            operation_id,
            event_id,
            current.session_id if current else None,
            current.event_kind
            if current
            else (
                EventKind.IMPORTANT_EXPERIENCE
                if patch.get("chat_followup", {}).get("category") == "cared"
                else EventKind.TASK
            ),
            kind,
            revision,
            patch,
            actor,
            generation_id,
            proposal_id,
            now,
        )
        if not uow.knowledge.add_event_operation(operation):
            raise ValueError("event operation conflict")
        state = EventCurrentState(
            event_id,
            operation.session_id,
            operation.event_kind,
            status,
            revision,
            payload,
            operation_id,
            current.created_at if current else now,
            now,
        )
        uow.knowledge.put_event_state(state, revision - 1)
        schedule = uow.reminders.get_schedule(event_id)
        if schedule is not None:
            schedule_status = {
                "cancelled": ReminderScheduleStatus.CANCELLED,
                "completed": ReminderScheduleStatus.COMPLETED,
            }.get(status.value, schedule.status)
            exact_time = payload.get("scheduled_time")
            scheduled_at = (
                datetime.fromisoformat(exact_time.replace("Z", "+00:00"))
                if exact_time
                else schedule.scheduled_at
            )
            if status is EventStatus.ACTIVE:
                if not exact_time:
                    schedule_status = ReminderScheduleStatus.STALE
                elif (
                    kind is EventOperationKind.REOPEN
                    or scheduled_at != schedule.scheduled_at
                    or schedule.status is ReminderScheduleStatus.STALE
                ):
                    schedule_status = ReminderScheduleStatus.SCHEDULED
            revised = replace(
                schedule,
                event_revision=revision,
                title=payload.get("title", schedule.title),
                scheduled_at=scheduled_at,
                status=schedule_status,
                updated_at=now,
            )
            uow.reminders.put_schedule(revised)
            uow.changes.append(
                "reminder", event_id, revision, "upsert", _schedule_projection(revised)
            )
        for r in payload.get("chat_followup", {}).get("evidence", []):
            uow.knowledge.add_evidence_link(
                new_ulid(),
                "event",
                event_id,
                revision,
                "chat_record",
                r["evidence_key"],
                r["record_revision"],
                now.isoformat(),
            )
        uow.audit.append(
            "chat.followup.applied",
            actor,
            "event",
            event_id,
            {"revision": revision, "operation": kind.value},
        )
        from .chat_followup_projection import publish

        publish(
            uow, state, uow.followups.source(payload["chat_followup"]["source_key"])
        )
        return state

    def save_job(self, job_id, state, value):
        with self.uow_factory() as uow:
            uow.followups.save_job(job_id, state, value)

    def job(self, job_id):
        with self.uow_factory().reading() as uow:
            return uow.followups.job(job_id)

    def jobs(self, limit=30):
        with self.uow_factory().reading() as uow:
            return uow.followups.jobs(limit)
