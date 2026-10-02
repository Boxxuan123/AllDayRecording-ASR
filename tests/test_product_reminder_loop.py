"""PRODUCT_REMINDER_E2E_TEST: synthetic, isolated SQLite; no speaker learning."""
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import pytest

from tests import test_v33_intelligent_reminders as seed
from allday_asr.v3.application import ReminderExtractionService, MobileSyncService
from allday_asr.v3.application.mobile_sync import UtteranceCorrectionOperationHandler
from allday_asr.v3.domain import ClientOperation, SyncRequest, new_ulid
from allday_asr.v3.domain.reminder_time import explicit_task


@pytest.fixture
def env():
    instance = seed.V33IntelligentReminderTests()
    instance.setUp()
    instance.current_time = datetime(2026, 10, 2, 1, tzinfo=timezone.utc)
    with instance.database.transaction() as c:
        c.execute("UPDATE utterances SET text=?,start_at=?,end_at=? WHERE utterance_id=?",
            ("我明天下午三点给老师发材料", "2026-10-01T13:00:00Z", "2026-10-01T13:00:01Z", seed.UTTERANCE_ID))
    instance.extraction = ReminderExtractionService(instance.factory, instance.reminders, None,
        now=lambda: instance.current_time)
    instance.sync = MobileSyncService(instance.factory,
        operation_handler=UtteranceCorrectionOperationHandler(now=lambda: instance.current_time),
        now=lambda: instance.current_time)
    yield instance
    instance.tearDown()


def candidate(env):
    return env.extraction.extract_product(seed.SESSION_ID)["candidates"][0]


def operation(env, kind, payload, revision=None, operation_id=None):
    command = ClientOperation(operation_id or new_ulid(), kind, revision, payload)
    response = env.sync.synchronize("computer-1", SyncRequest(5, "cursor-0", (command,), 100))
    return command, response


def confirmed(env):
    item = candidate(env)
    command, response = operation(env, "reminder.review", {"candidate_id": item["candidate_id"], "action": "confirm"})
    assert response.receipts[0].status == "applied"
    return env.reminders.list_schedules()[0], command, response


def test_recording_anchor_and_provenance(env):
    item = candidate(env)
    assert item["scheduled_at"] == "2026-10-02T07:00:00.000000Z"
    assert item["status"] == "pending_confirmation"
    assert env.reminders.list_schedules() == ()
    assert item["input_scope"]["recorded_at"] == "2026-10-01T13:00:00Z"
    assert item["input_scope"]["time_expression"] == "明天下午三点"
    assert item["input_scope"]["timezone"] == "Asia/Singapore"
    assert item["input_scope"]["source_text"] == "我明天下午三点给老师发材料"
    assert item["evidence"][0]["utterance_id"] == seed.UTTERANCE_ID


def test_same_source_three_runs_and_concurrent_retry_single_candidate(env):
    with ThreadPoolExecutor(max_workers=3) as pool:
        values = list(pool.map(lambda _: candidate(env)["candidate_id"], range(3)))
    assert len(set(values)) == 1
    assert len(env.reminders.list_candidates()) == 1


def test_new_candidate_review_has_no_existing_source_revision(env):
    item = candidate(env)
    assert item["expected_revision"] == 0
    with env.factory().reading() as uow:
        review = next(row for row in uow.desktop.list_reviews(50)
                      if row["source_id"] == item["candidate_id"])
    assert review["source_revision"] is None
    assert review["context"]["source_text"] == "我明天下午三点给老师发材料"
    assert review["context"]["scheduled_at"] == item["scheduled_at"]


@pytest.mark.parametrize("text", [
    "我昨天已经发过了", "不用发了", "这个取消", "我本来准备明天发，但是现在不用了",
    "不用提醒我发材料了", "我明天下午三点不用发材料", "明天下午三点发材料",
    "我明天三点发材料", "我每天下午三点发材料", "我明天下午三点发材料吗？",
    "我明天下午三点不发材料", "我明天下午三点老师发材料",
])
def test_negative_completed_recurring_or_unclear_actor_cannot_create(env, text):
    with env.database.transaction() as c:
        c.execute("UPDATE utterances SET text=?", (text,))
    assert env.extraction.extract_product(seed.SESSION_ID)["candidates"] == []
    assert env.reminders.list_schedules() == ()


def test_unknown_speaker_not_silently_self(env):
    with env.database.transaction() as c:
        c.execute("UPDATE utterances SET identity='unknown'")
    assert env.extraction.extract_product(seed.SESSION_ID)["candidates"] == []


def test_confirmation_sync_restart_and_http_retry(env):
    schedule, command, response = confirmed(env)
    replay = env.sync.synchronize("computer-1", SyncRequest(5, "cursor-0", (command,), 100))
    assert replay.receipts == response.receipts
    assert len(env.reminders.list_schedules()) == 1
    assert len(env.reminders.list_candidates()) == 1
    assert any(change.resource_type == "reminder" for change in response.changes)
    from allday_asr.v3.adapters.sqlite import V3Database, SqliteUnitOfWork
    with SqliteUnitOfWork(V3Database(env.database.path)).reading() as uow:
        assert uow.reminders.get_schedule(schedule["event_id"]).status == "scheduled"
    assert candidate(env)["candidate_id"] == schedule["source_candidate_id"]


def test_candidate_edit_ignore_and_no_reappearance(env):
    item = candidate(env)
    _, response = operation(env, "reminder.review", {"candidate_id": item["candidate_id"],
        "action": "edit", "scheduled_at": "2026-10-02T08:00:00+00:00", "title": "给老师发最终材料"})
    assert response.receipts[0].status == "applied"
    schedule = env.reminders.list_schedules()[0]
    assert schedule["scheduled_at"] == "2026-10-02T08:00:00.000000Z"
    assert candidate(env)["status"] == "modified"


def test_ignore_survives_reprocessing_and_restart(env):
    item = candidate(env)
    _, response = operation(env, "reminder.review", {"candidate_id": item["candidate_id"], "action": "ignore"})
    assert response.receipts[0].status == "applied"
    assert candidate(env)["status"] == "ignored"
    assert len(env.reminders.list_candidates()) == 1
    assert env.reminders.list_schedules() == ()


def test_old_source_replay_beyond_recent_candidate_window(env):
    from dataclasses import replace
    original_id = candidate(env)["candidate_id"]
    with env.factory() as uow:
        original = uow.reminders.get_candidate(original_id)
        proposal = uow.knowledge.get_proposal(original.proposal_id)
        for index in range(501):
            newer = replace(proposal, proposal_id=new_ulid())
            uow.knowledge.add_proposal(newer)
            uow.reminders.add_candidate(replace(original, candidate_id=new_ulid(),
                proposal_id=newer.proposal_id, created_at=original.created_at + timedelta(seconds=index + 1)))
    assert original_id not in {item["candidate_id"] for item in env.reminders.list_candidates(limit=500)}
    assert candidate(env)["candidate_id"] == original_id


@pytest.mark.parametrize("terminal,status", [("cancel", "cancelled"), ("complete", "completed")])
def test_reschedule_terminal_and_stale_revision(env, terminal, status):
    schedule, _, _ = confirmed(env)
    eid = schedule["event_id"]
    _, response = operation(env, "reminder.task", {"event_id": eid, "action": "reschedule",
        "scheduled_at": "2026-10-02T08:00:00Z"}, 1)
    assert response.receipts[0].status == "applied"
    assert env.reminders.schedule(eid)["event_revision"] == 2
    assert env.reminders.schedule(eid)["scheduled_at"] == "2026-10-02T08:00:00.000000Z"
    _, stale = operation(env, "reminder.task", {"event_id": eid, "action": terminal}, 1)
    assert stale.receipts[0].status == "conflict"
    _, response = operation(env, "reminder.task", {"event_id": eid, "action": terminal}, 2)
    assert response.receipts[0].status == "applied"
    assert env.reminders.schedule(eid)["status"] == status
    assert len(env.reminders.list_schedules()) == 1
    assert candidate(env)["status"] == "confirmed"


def test_stale_source_identity_due_rejected_before_mutation(env):
    item = candidate(env)
    with env.database.transaction() as c:
        c.execute("UPDATE utterances SET identity='unknown'")
    _, response = operation(env, "reminder.review", {"candidate_id": item["candidate_id"], "action": "confirm"})
    assert response.receipts[0].status == "conflict"
    assert env.reminders.list_schedules() == ()


def test_event_changed_outside_reminder_rejects_task_with_durable_conflict(env):
    from dataclasses import replace
    schedule, _, _ = confirmed(env)
    with env.factory() as uow:
        state = uow.knowledge.get_event(schedule["event_id"])
        uow.knowledge.put_event_state(replace(state, revision=state.revision + 1), state.revision)
    command, response = operation(env, "reminder.task", {"event_id": schedule["event_id"], "action": "complete"}, 1)
    assert response.receipts[0].status == "conflict"
    replay = env.sync.synchronize("computer-1", SyncRequest(5, "cursor-0", (command,), 100))
    assert replay.receipts == response.receipts
    assert env.reminders.schedule(schedule["event_id"])["status"] == "scheduled"


def test_unexpected_storage_failure_rolls_back_task_and_receipt(env, monkeypatch):
    item = candidate(env)
    from allday_asr.v3.application.reminder_apply import ReminderApplyMixin
    def fail(*args, **kwargs):
        raise RuntimeError("disk failure")
    monkeypatch.setattr(ReminderApplyMixin, "_apply_candidate", fail)
    command = ClientOperation(new_ulid(), "reminder.review", None, {"candidate_id": item["candidate_id"], "action": "confirm"})
    with pytest.raises(RuntimeError, match="disk failure"):
        env.sync.synchronize("computer-1", SyncRequest(5, "cursor-0", (command,), 100))
    assert env.knowledge.list_events(seed.SESSION_ID) == ()
    assert env.reminders.candidate(item["candidate_id"])["status"] == "pending_confirmation"
    with env.factory().reading() as uow:
        assert uow.mobile_sync.find_operation(command.operation_id) is None


@pytest.mark.parametrize("text,expected", [
    ("我今晚9点20提醒我检查测试结果", "2026-10-01T21:20:00+08:00"),
    ("我后天上午十点半发材料", "2026-10-03T10:30:00+08:00"),
    ("我2026年10月4日下午四点发材料", "2026-10-04T16:00:00+08:00"),
])
def test_supported_explicit_time_forms(text, expected):
    result = explicit_task(text, "2026-10-01T13:00:00Z", "Asia/Singapore")
    assert result[1].isoformat() == expected


def test_overdue_delayed_sync_does_not_roll_forward(env):
    env.current_time += timedelta(days=2)
    assert env.extraction.extract_product(seed.SESSION_ID)["candidates"] == []
