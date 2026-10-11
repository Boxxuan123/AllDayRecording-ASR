"""Atomic phone commands and first schedule use real existing Event/UOW."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import pytest
from tests.test_chat_followups import env as env
from tests.test_chat_followup_review import review_job, accept
from allday_asr.v3.domain.device_sync import ClientOperation
from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.application.chat_phone_operations import apply_chat_operation
from allday_asr.v3.adapters.sqlite.reminder_repository import SqliteReminderRepository


def accepted(env):
    ctl, review, _ = review_job(env)
    accept(ctl, review)
    return ctl, ctl.service.list()["items"][0]


@pytest.mark.parametrize(
    "changes",
    [
        {"at": "2030-10-12", "confirm_current": True},
        {"at": "2030-02-31T09:00+08:00", "confirm_current": True},
        {"at": "2030-01-01T09:00+08:60", "confirm_current": True},
        {"at": "2030-10-12T09:00:00+08:00"},
        {"at": "2030-10-12T09:00:00", "confirm_current": True},
    ],
)
def test_missing_explicit_time_or_confirmation_rejected_without_mutation(env, changes):
    ctl, item = accepted(env)
    op = ClientOperation(
        new_ulid(),
        "chat.followup",
        item["revision"],
        {"event_id": item["event_id"], "action": "enable_reminder", "changes": changes},
    )
    with env.factory() as uow:
        receipt = apply_chat_operation(op, uow, datetime.now(timezone.utc))
    assert receipt.status.value == "rejected"
    after = ctl.service.list()["items"][0]
    assert after["revision"] == item["revision"] and after["payload"] == item["payload"]
    with env.factory().reading() as uow:
        assert uow.reminders.get_schedule(item["event_id"]) is None
    ctl.close()


def test_schedule_failure_rolls_back_event_and_source_but_receipt_can_persist(env):
    ctl, item = accepted(env)
    due = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    op = ClientOperation(
        new_ulid(),
        "chat.followup",
        item["revision"],
        {
            "event_id": item["event_id"],
            "action": "enable_reminder",
            "changes": {"at": due, "confirm_current": True},
        },
    )
    with patch.object(
        SqliteReminderRepository,
        "put_schedule",
        side_effect=ValueError("synthetic storage failure"),
    ):
        with env.factory() as uow:
            receipt = apply_chat_operation(op, uow, datetime.now(timezone.utc))
    assert receipt.status.value == "rejected"
    after = ctl.service.list()["items"][0]
    assert after["revision"] == item["revision"] and after["payload"] == item["payload"]
    ctl.close()


def test_date_only_reschedule_suspends_original_reminder_until_explicit_time(env):
    from allday_asr.v3.application.reminders import IntelligentReminderService

    ctl, item = accepted(env)
    ctl.service.reminders = IntelligentReminderService(env.factory, None)
    due = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    enabled = ctl.service.act(
        item["source_key"],
        "enable_reminder",
        {"at": due, "confirm_current": True},
        item["revision"],
    )
    edited = ctl.service.act(
        item["source_key"], "edit", {"date": "2030-10-12"}, enabled["revision"]
    )
    with env.factory().reading() as uow:
        schedule = uow.reminders.get_schedule(item["event_id"])
        assert schedule.status.value == "stale"
        assert schedule.scheduled_at.isoformat() == due
        state = uow.knowledge.get_event(item["event_id"])
        assert state.payload["scheduled_time"] is None
        assert state.payload["chat_followup"]["time"]["at"] is None
    ctl.service.act(
        item["source_key"],
        "enable_reminder",
        {"at": due, "confirm_current": True},
        edited["revision"],
    )
    with env.factory().reading() as uow:
        assert uow.reminders.get_schedule(item["event_id"]).status.value == "scheduled"
        assert (
            len(
                uow.reminders.list_schedules(
                    status=None, session_id=None, due_before=None, limit=100
                )
            )
            == 1
        )
    ctl.close()
