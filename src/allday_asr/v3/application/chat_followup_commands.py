"""Human followup decisions on the existing Event aggregate and reminder UOW."""

from datetime import datetime
import json
from allday_asr.v3.domain.knowledge import EventKind, EventOperationKind, EventStatus
from allday_asr.v3.ports.chat_data import ChatDataError


class ChatFollowupCommands:
    def act(
        self, key, action, changes=None, expected_revision=None, *, actor="desktop-user"
    ):
        changes = changes or {}
        if action not in {
            "confirm",
            "complete",
            "cancel",
            "ignore",
            "reopen",
            "edit",
            "link",
            "accept_change",
            "dismiss_change",
            "enable_reminder",
        }:
            raise ChatDataError("INVALID_FOLLOWUP_ACTION", 400)
        allowed = {
            "title",
            "date",
            "at",
            "timezone",
            "category",
            "event_id",
            "confirm_current",
        }
        if set(changes) - allowed:
            raise ChatDataError("INVALID_FOLLOWUP_EDIT", 400)
        with self.uow_factory() as uow:
            source = uow.followups.source(key)
            if not source:
                raise ChatDataError("NOT_FOUND", 404)
            current = uow.knowledge.get_event(source["event_id"])
            if expected_revision is None or current.revision != expected_revision:
                raise ChatDataError("FOLLOWUP_REVISION_CONFLICT", 409)
            chat = dict(current.payload.get("chat_followup", {}))
            patch = {}
            kind = {
                "complete": "complete",
                "cancel": "cancel",
                "ignore": "cancel",
                "reopen": "reopen",
            }.get(action, "update")
            if action == "link":
                target = uow.knowledge.get_event(changes.get("event_id", ""))
                if (
                    not target
                    or target.session_id is None
                    or target.event_kind
                    not in {
                        EventKind.TASK,
                        EventKind.REQUEST,
                        EventKind.COMMITMENT,
                        EventKind.APPOINTMENT,
                    }
                ):
                    raise ChatDataError("INVALID_RECORDING_TASK_LINK", 422)
                if target.status is not EventStatus.ACTIVE:
                    raise ChatDataError("RECORDING_TASK_ENDED", 409)
                target_chat = target.payload.get("chat_followup", {})
                if target_chat and target_chat.get("conversation_key") != chat.get(
                    "conversation_key"
                ):
                    raise ChatDataError("FOLLOWUP_CONVERSATION_MISMATCH", 409)
                # Evidence enrichment never rewrites the recorded task's title/time/owner.
                merged = {r["evidence_key"]: r for r in target_chat.get("evidence", [])}
                merged.update({r["evidence_key"]: r for r in chat.get("evidence", [])})
                chat["evidence"] = list(merged.values())
                chat["linked_recording_event_id"] = target.event_id
                self._append(
                    uow,
                    target,
                    {"chat_followup": chat},
                    EventOperationKind.UPDATE,
                    actor,
                )
                self._append(
                    uow,
                    current,
                    {"linked_to": target.event_id},
                    EventOperationKind.CANCEL,
                    actor,
                )
                source["event_id"] = target.event_id
                source["human_override"] = 1
                uow.followups.save_source(key, source)
                return {"event_id": target.event_id, "effect": "linked"}
            if action in {"accept_change", "dismiss_change"}:
                if not source["conflict_json"]:
                    raise ChatDataError("NO_FOLLOWUP_CONFLICT", 409)
                if action == "accept_change":
                    proposed = json.loads(source["conflict_json"])
                    if proposed.get("change_kind") == "evidence_replaced":
                        raise ChatDataError("FOLLOWUP_EVIDENCE_CHANGED", 409)
                    previous_time = chat.get("time", {})
                    chat = {
                        **chat,
                        **{
                            k: proposed[k]
                            for k in ("action", "time", "latest_sent_at", "uncertainty")
                            if k in proposed
                        },
                        "evidence": proposed["evidence"],
                    }
                    merged = {
                        r["evidence_key"]: r
                        for r in current.payload.get("chat_followup", {}).get(
                            "evidence", []
                        )
                    }
                    merged.update({r["evidence_key"]: r for r in chat["evidence"]})
                    chat["evidence"] = list(merged.values())
                    kind = chat["action"] if chat["action"] != "create" else "update"
                    if uow.reminders.get_schedule(current.event_id):
                        if chat.get("time", {}).get("at"):
                            patch["scheduled_time"] = chat["time"]["at"]
                        elif (
                            kind not in {"complete", "cancel"}
                            and chat.get("time") != previous_time
                        ):
                            # A date-only reschedule suspends the old Calendar time.
                            patch["scheduled_time"] = None
                    source["latest_sent_at"] = chat["latest_sent_at"]
                source["conflict_json"] = None
            if action == "edit":
                if not changes:
                    raise ChatDataError("INVALID_FOLLOWUP_EDIT", 400)
                if "title" in changes:
                    if (
                        not isinstance(changes["title"], str)
                        or not changes["title"].strip()
                    ):
                        raise ChatDataError("INVALID_FOLLOWUP_TITLE", 400)
                    patch["title"] = changes["title"].strip()
                if any(k in changes for k in ("date", "at", "timezone")):
                    info = {
                        **chat.get("time", {}),
                        **{
                            k: changes[k]
                            for k in ("date", "at", "timezone")
                            if k in changes
                        },
                    }
                    if "date" in changes and "at" not in changes:
                        info["at"] = None
                    if info.get("date"):
                        try:
                            datetime.strptime(info["date"], "%Y-%m-%d")
                        except (ValueError, TypeError):
                            raise ChatDataError("INVALID_FOLLOWUP_TIME", 400) from None
                    if info.get("at"):
                        try:
                            parsed = datetime.fromisoformat(
                                info["at"].replace("Z", "+00:00")
                            )
                        except (ValueError, TypeError):
                            raise ChatDataError("INVALID_FOLLOWUP_TIME", 400) from None
                        if not parsed.tzinfo:
                            raise ChatDataError("INVALID_FOLLOWUP_TIME", 400)
                    info["precision"] = (
                        "minute"
                        if info.get("at")
                        else ("day" if info.get("date") else "unknown")
                    )
                    info["manually_edited"] = True
                    chat["time"] = info
                    if uow.reminders.get_schedule(current.event_id):
                        patch["scheduled_time"] = info.get("at")
            if action == "enable_reminder":
                if (
                    not self.reminders
                    or changes.get("confirm_current") is not True
                    or not changes.get("at")
                ):
                    raise ChatDataError("EXPLICIT_REMINDER_CONFIRMATION_REQUIRED", 422)
                if (
                    source["ignored"]
                    or current.status is not EventStatus.ACTIVE
                    or chat.get("actor_is_from_self") is not True
                ):
                    raise ChatDataError("CONFIRM_CURRENT_SELF_TASK_FIRST", 409)
                try:
                    due = datetime.fromisoformat(changes["at"].replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    raise ChatDataError(
                        "EXPLICIT_FUTURE_REMINDER_TIME_REQUIRED", 422
                    ) from None
                if due.tzinfo is None or due <= self.now():
                    raise ChatDataError("EXPLICIT_FUTURE_REMINDER_TIME_REQUIRED", 422)
                chat.update(
                    user_confirmed=True, historical=False, progress_unknown=False
                )
                chat["time"] = {
                    **chat.get("time", {}),
                    "at": due.isoformat(),
                    "date": due.date().isoformat(),
                    "precision": "minute",
                    "manually_edited": True,
                    "uncertainty": None,
                }
                patch["scheduled_time"] = due.isoformat()
            if action == "confirm":
                category = changes.get("category", chat.get("category", "pending"))
                if category not in {"self", "waiting", "pending"}:
                    raise ChatDataError("INVALID_FOLLOWUP_CATEGORY", 400)
                chat["category"] = category
                chat["historical"] = False
                chat["progress_unknown"] = False
                chat["user_confirmed"] = True
            if action == "ignore":
                source["ignored"] = 1
            if action == "reopen":
                source["ignored"] = 0
            source["human_override"] = 1
            patch["chat_followup"] = chat
            if current.status is not EventStatus.ACTIVE and kind == "update":
                # Metadata edits do not silently reopen a closed item.
                pass
            uow.followups.save_source(key, source)
            state = self._append(uow, current, patch, EventOperationKind(kind), actor)
            if action == "enable_reminder":
                self.reminders.enable_chat_reminder(
                    uow, state, changes["at"], self.now()
                )
            return {
                "event_id": state.event_id,
                "revision": state.revision,
                "effect": action,
            }
