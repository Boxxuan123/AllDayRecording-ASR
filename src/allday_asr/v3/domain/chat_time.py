"""Shared explicit-input grammar: real calendar dates, clock and offset fields."""

import re
from datetime import datetime

EXPLICIT_TIME = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})"
)


def explicit_time(value):
    if not isinstance(value, str) or not EXPLICIT_TIME.fullmatch(value):
        raise ValueError("explicit time requires ISO calendar fields and timezone")
    offset = value[-6:]
    if offset[0] in {"+", "-"} and (int(offset[1:3]) > 23 or int(offset[4:]) > 59):
        raise ValueError("invalid timezone offset")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def proposed_time(previous, proposed, latest_sent_at):
    """Default nulls mean no change; a new literal time expression may replace time."""
    info = proposed.get("time", {})
    expression = info.get("expression", "")
    if not expression:
        return previous
    # Repeating the original deadline in linking context must not undo human edits.
    refs = [
        e
        for e in proposed.get("evidence", [])
        if expression in e.get("quote", "")
        and (not info.get("record_id") or e["record_id"] == info["record_id"])
        and e.get("sent_at") is not None
        and (latest_sent_at is None or e["sent_at"] > latest_sent_at)
    ]
    return info if len(refs) == 1 else previous
