"""A comparison target is not an asserted utterance period; all positive periods stay checked."""

import pytest
from allday_asr.v3.domain.recall_event_time import validate_message_dayparts


@pytest.mark.parametrize(
    "text,hour",
    [
        ("记录在10月11日上午，比你说的12日晚上早。", 10),
        ("讨论在10月11日下午，比10月12日晚上早一天。", 16),
        ("发言在下午，比预期的2026-10-12晚上更早。", 15),
        ("这条在晚上，比昨天上午晚。", 20),
    ],
)
def test_dated_comparison_object_does_not_set_message_daypart(text, hour):
    validate_message_dayparts(text, hour)


@pytest.mark.parametrize(
    "text,hour",
    [
        ("记录在晚上，比12日上午晚。", 10),
        ("记录比12日晚上早，但实际在晚上发出。", 10),
        ("不是昨天晚上，而是今天晚上发出的。", 16),
        ("记录在12日晚上。", 16),
        ("比上午好，记录在晚上。", 9),
    ],
)
def test_comparison_does_not_hide_affirmative_wrong_period(text, hour):
    with pytest.raises(ValueError, match="daypart disagrees"):
        validate_message_dayparts(text, hour)
