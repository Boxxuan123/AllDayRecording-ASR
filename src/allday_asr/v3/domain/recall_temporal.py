"""One adjacent-day lead for approximate natural dates; explicit filters never widen."""

import re
from copy import deepcopy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def nearby_scope(scope, payload):
    if any(k in payload for k in ("sent_from", "sent_to")) or payload.get("keyword"):
        return None
    question = payload.get("question", "")
    if not re.search(r"昨天|前天|昨晚", question) or re.search(
        r"\d{4}[-/年]|\d{1,2}月\d", question
    ):
        return None
    if scope["sent_to"] - scope["sent_from"] > 86400:
        return None
    zone = ZoneInfo(scope["timezone"])
    day = datetime.fromtimestamp(scope["sent_from"], zone).date() - timedelta(days=1)
    first = datetime.combine(day, datetime.min.time(), zone)
    last = first + timedelta(days=1)
    return {
        **deepcopy(scope),
        "sent_from": int(first.timestamp()),
        "sent_to": int(last.timestamp()) - 1,
    }


def time_notice(requested, actual):
    zone = ZoneInfo(actual["timezone"])
    a = datetime.fromtimestamp(requested["sent_from"], zone).strftime("%Y-%m-%d %H:%M")
    b = datetime.fromtimestamp(actual["sent_from"], zone).strftime("%Y-%m-%d")
    return f"原问题时间从 {a} 解释；指定时段未找到对应交流，临近原文实际在 {b}。以下按实际消息日期归纳，不能据此认定原时段发生了这些事。"
