"""Reported and prospective time belongs to speech content, not to its sending period."""

import pytest
from allday_asr.v3.domain.recall_event_time import validate_message_dayparts


@pytest.mark.parametrize(
    "text,quotes",
    [
        ("下午，周启说“转接失败”，上午还正常。", ["上午还能接通"]),
        ("他判断是会议室设备问题，建议等晚上再试。", ["等晚上再试试"]),
        ("他下午说准备晚上开会。", ["准备晚上开会"]),
        ("她提到“上午我去过书店”。", ["上午我去过书店"]),
    ],
)
def test_source_supported_reported_period_is_not_utterance_period(text, quotes):
    validate_message_dayparts(text, 14, quotes)


@pytest.mark.parametrize(
    "text,quotes",
    [
        ("上午周启说转接失败。", ["我上午还正常"]),
        ("发言在上午。", ["上午还正常"]),
        ("下午说了原因，上午同事又回复了。", ["上午还正常"]),
        ("他说上午还正常，但记录在晚上发出。", ["上午还正常", "等晚上再试"]),
        ("他判断是设备问题，建议等晚上再试。", ["今天还不能用"]),
        ("他上午说准备晚上开会。", ["准备晚上开会"]),
        ("他先说了原因，记录在2026-10-10上午。", ["我上午还正常"]),
        ("他下午说上午还好，上午她又说了进展。", ["上午还好"]),
    ],
)
def test_reported_period_does_not_hide_false_utterance_time(text, quotes):
    with pytest.raises(ValueError, match="daypart disagrees"):
        validate_message_dayparts(text, 14, quotes)
