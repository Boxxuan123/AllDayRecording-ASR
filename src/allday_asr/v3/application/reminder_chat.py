"""First chat reminder on the existing ReminderSchedule and Event identity."""

from datetime import datetime
from allday_asr.v3.domain.reminders import (
    ReminderSchedule,
    ReminderScheduleStatus,
    CommitmentDirection,
)
from allday_asr.v3.domain.hashing import canonical_json_sha256
from allday_asr.v3.ports.chat_data import ChatDataError
from .reminder_support import _schedule_projection


class ChatReminderMixin:
    def enable_chat_reminder(self, uow, current, scheduled_at, now):
        if current.session_id is not None or not current.payload.get("chat_followup"):
            raise ChatDataError("NOT_INDEPENDENT_CHAT_TASK", 409)
        chat = current.payload["chat_followup"]
        if (
            current.status.value != "active"
            or chat.get("actor_is_from_self") is not True
            or not chat.get("user_confirmed")
            or chat.get("historical")
        ):
            raise ChatDataError("CONFIRM_CURRENT_SELF_TASK_FIRST", 409)
        due = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
        if due.tzinfo is None or due <= now:
            raise ChatDataError("EXPLICIT_FUTURE_REMINDER_TIME_REQUIRED", 422)
        schedule = uow.reminders.get_schedule(current.event_id)
        if schedule is not None:
            return schedule
        schedule = ReminderSchedule(
            current.event_id,
            None,
            current.revision,
            None,
            current.payload["title"],
            "self",
            CommitmentDirection.SELF_TO_OTHER,
            (),
            due,
            None,
            ReminderScheduleStatus.SCHEDULED,
            canonical_json_sha256({"chat_event_id": current.event_id}),
            now,
            now,
        )
        uow.reminders.put_schedule(schedule)
        uow.changes.append(
            "reminder",
            current.event_id,
            current.revision,
            "upsert",
            _schedule_projection(schedule),
        )
        return schedule
