"""Source-relative dates; query scope parsing remains deliberately stricter."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo
from .recall_query import DATE, RELATIVE, dates


def source_relative_period(quote, timezone, sent_at):
    labels = RELATIVE.findall(quote)
    if len(labels) <= 1:
        return dates(quote, timezone, sent_at)[0]
    # Only an explicitly stated, contiguous set of calendar days can be combined.
    # Never fill an unmentioned day, mix absolute dates, or resolve alternatives.
    if (
        any(x not in {"今天", "昨天", "前天", "大前天"} for x in labels)
        or re.search(DATE, quote)
        or re.search(r"或|还是", quote)
    ):
        raise ValueError("ambiguous source-relative dates")
    dates(RELATIVE.sub("", quote), timezone, sent_at)
    periods = [dates(label, timezone, sent_at)[0] for label in labels]
    zone = ZoneInfo(timezone)
    days = {datetime.fromtimestamp(p[0], zone).date() for p in periods}
    if (max(days) - min(days)).days + 1 != len(days):
        raise ValueError("source-relative days are not contiguous")
    return min(p[0] for p in periods), max(p[1] for p in periods)
