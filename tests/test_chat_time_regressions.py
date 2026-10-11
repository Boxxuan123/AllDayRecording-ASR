"""No models: ordinary progress retains Calendar binding; explicit changes replace it."""

import pytest
from tests.test_chat_followups import env as env, candidate
from tests.test_chat_phone_operations import accepted
from tests.chat_fixture import record
from allday_asr.v3.application.reminders import IntelligentReminderService
from allday_asr.v3.domain.chat_time import explicit_time


@pytest.mark.parametrize(
    "expression,date,at,text",
    [
        ("", None, None, "资料已经整理好，还在等老师确认"),
        (
            "2030年10月13日14:00",
            "2030-10-13",
            "2030-10-13T14:00:00+08:00",
            "改到2030年10月13日14:00提交",
        ),
        ("2030年10月13日", "2030-10-13", None, "改到2030年10月13日，时间待定"),
        ("取消时间", None, None, "资料还要交，取消时间，等通知"),
    ],
)
def test_accept_progress_time_semantics(env, expression, date, at, text):
    ctl, item = accepted(env)
    ctl.service.reminders = IntelligentReminderService(env.factory, None)
    ctl.service.act(
        item["source_key"],
        "enable_reminder",
        {"at": "2030-10-12T09:00:00+08:00", "confirm_current": True},
        item["revision"],
    )
    before = ctl.service.list()["items"][0]
    old = ctl.chat.cache.record(before["chat"]["anchor_id"])
    new = record(987, text, sent=old["sent_at"] + 100)
    new.update(is_from_self=True, self_identity_basis="fixture.direction")
    c = candidate(
        old,
        action="update",
        target_event_id=item["event_id"],
        time_expression=expression,
        due_date=date,
        due_at=at,
        evidence=[
            {"record_id": old["record_id"], "quote": old["text"]},
            {"record_id": new["record_id"], "quote": text},
        ],
    )
    assert (
        ctl.service.apply(
            c, [old, new], "dataset", ctl.links(), timezone_name="Asia/Shanghai"
        )["effect"]
        == "human_conflict"
    )
    current = ctl.service.list()["items"][0]
    ctl.service.act(item["source_key"], "accept_change", {}, current["revision"])
    after = ctl.service.list()["items"][0]
    assert after["event_id"] == item["event_id"]
    assert after["chat"]["user_confirmed"]
    with env.factory().reading() as uow:
        schedule = uow.reminders.get_schedule(item["event_id"])
        assert schedule.status.value == (
            "scheduled" if not expression or at else "stale"
        )
        if not expression:
            assert after["chat"]["time"] == before["chat"]["time"]
            assert schedule.scheduled_at == explicit_time("2030-10-12T09:00:00+08:00")
        elif at:
            assert schedule.scheduled_at == explicit_time(at)
        else:
            assert after["chat"]["time"]["at"] is None
    ctl.close()


@pytest.mark.parametrize(
    "value",
    [
        "2030-02-31T09:00+08:00",
        "2030-02-29T09:00Z",
        "2032-04-31T09:00Z",
        "2030-01-01T24:00Z",
        "2030-01-01T09:60Z",
        "2030-01-01T09:00:60Z",
        "2030-01-01T09:00+08:60",
        "2030-01-01T09:00+24:00",
        "0000-01-01T09:00Z",
    ],
)
def test_invalid_real_calendar_inputs(value):
    with pytest.raises(ValueError):
        explicit_time(value)


@pytest.mark.parametrize(
    "value",
    [
        "2032-02-29T09:00+08:00",
        "2030-01-01T09:00:59Z",
        "2030-01-01T09:00:00.123456-05:30",
    ],
)
def test_valid_explicit_inputs(value):
    assert explicit_time(value).tzinfo


def test_shared_calendar_contract_fixture():
    import json
    from pathlib import Path

    fixture = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "contracts/v3/fixtures/explicit-times.json"
        ).read_text()
    )
    for value in fixture["valid"]:
        assert explicit_time(value).tzinfo
    for value in fixture["invalid"]:
        with pytest.raises(ValueError):
            explicit_time(value)
