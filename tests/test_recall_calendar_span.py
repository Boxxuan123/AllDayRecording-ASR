"""Calendar shorthand must retain its endpoint; no midnight precision is invented."""

from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from allday_asr.v3.domain.recall_query import dates
from tests.recall_scenario_driver import contact, message, run_scenario, selected_ids

NOW = 1792110900
ZONE = ZoneInfo("Asia/Singapore")


def day(d):
    return int(datetime(2025, 6, d, tzinfo=ZONE).timestamp())


@pytest.mark.parametrize("connector", ["到", "至", "～"])
def test_day_only_endpoint_inherits_only_written_month_and_year(connector):
    bounds, rest = dates(
        "2025年6月8日" + connector + "9日园艺群交流了什么？", "Asia/Singapore", NOW
    )
    assert bounds == (day(8), day(10) - 1)
    assert "9日" not in rest


@pytest.mark.parametrize(
    "question",
    [
        "2025年6月9日到8日的交流",
        "2025年2月27日至30日的交流",
        "2025年6月8日下午到9日上午的交流",
    ],
)
def test_invalid_or_unhandled_part_day_endpoints_are_not_silently_lost(question):
    with pytest.raises(ValueError):
        dates(question, "Asia/Singapore", NOW)


def test_approximate_night_keeps_both_dates_with_explicit_coverage_note(tmp_path):
    records = [
        message(
            951,
            "报名先等等，还没决定。",
            "actor",
            "garden",
            hour=23,
            minute=55,
            day=day(8),
            label="云桐",
        ),
        message(
            952,
            "我再问清楚，不算已经报名。",
            "actor",
            "garden",
            hour=0,
            minute=5,
            day=day(9),
            label="云桐",
        ),
        message(
            953,
            "范围之后的记录不能进来。",
            "actor",
            "garden",
            hour=0,
            day=day(10),
            label="云桐",
        ),
        message(954, "白天无关闲聊", "actor", "garden", hour=12, day=day(8), label="云桐"),
        message(955, "次日白天无关闲聊", "actor", "garden", hour=12, day=day(9), label="云桐"),
    ]
    run = run_scenario(
        tmp_path,
        dict(
            question="总结2025年6月8日深夜到9日刚过零点，云桐在园艺交流群里谈活动报名的交流；最终报名能确认吗？",
            contacts=[contact("qq", "garden", "园艺交流群", kind="group")],
            records=records,
            protocol_citation_probe=True,
        ),
    )
    assert {"rec:fixture-951", "rec:fixture-952"} <= selected_ids(run)
    assert not {"rec:fixture-953", "rec:fixture-954", "rec:fixture-955"} & selected_ids(run)
    assert run["internal"]["scope"]["sent_from"] == day(8) + 18 * 3600
    assert run["internal"]["scope"]["sent_to"] == day(9) + 6 * 3600 - 1
    assert run["wire"]
    note = run["internal"]["scope"].get("time_precision_note", "")
    assert note and "检索" in note and "精确" in note
    assert note in run["internal"]["coverage"]
    assert note in run["result"]["answer"]
