"""Conservative one-off Chinese commitments; relative dates use capture time."""
from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

VERSION = "explicit-self-reminder.1"
_NUMBER = r"[0-9零〇一二两三四五六七八九十]{1,3}"
_TIME = re.compile(
    rf"(?P<day>今天|今晚|明天|后天|\d{{4}}年\d{{1,2}}月\d{{1,2}}日)"
    rf"(?P<period>凌晨|早上|上午|中午|下午|晚上)?\s*"
    rf"(?P<hour>{_NUMBER})\s*(?:点|:|：)"
    rf"(?P<minute>半|{_NUMBER}(?:分)?)?"
)
_UNSAFE = re.compile(
    r"不|没|无需|取消|已经|已[经]?完成|发过|做完|本来|原本|"
    r"昨天|前天|每[天周月年]|每天|每星期|如果|假如|可能|也许|要不要|[?？]|吗"
)


def number(value: str) -> int:
    if value.isascii() and value.isdigit():
        return int(value)
    digits = {c: i for i, c in enumerate("零一二三四五六七八九")}
    digits.update({"〇": 0, "两": 2})
    if "十" in value:
        left, right = value.split("十")
        return (digits[left] if left else 1) * 10 + (digits[right] if right else 0)
    return digits[value]


def explicit_task(text: str, captured_at: str, timezone_name: str):
    """Return title, absolute due time and verbatim time expression, or None."""
    text = text.strip()
    if _UNSAFE.search(text) or not text.startswith("我"):
        return None
    matches = list(_TIME.finditer(text))
    if len(matches) != 1:
        return None
    match = matches[0]
    # Only an explicit first-person commitment, without intervening actors.
    if text[1:match.start()].strip() not in ("", "会", "要", "打算"):
        return None
    try:
        zone = ZoneInfo({"CST": "Asia/Singapore"}.get(timezone_name, timezone_name))
        anchor = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        if anchor.tzinfo is None:
            return None
        local = anchor.astimezone(zone)
        day = match["day"]
        if "年" in day:
            year, month, date = map(int, re.findall(r"\d+", day))
            local = local.replace(year=year, month=month, day=date)
        else:
            local += timedelta(days={"今天": 0, "今晚": 0, "明天": 1, "后天": 2}[day])
        hour = number(match["hour"])
        period = match["period"] or ("晚上" if day == "今晚" else "")
        if not 0 <= hour <= 23 or (not period and hour <= 12):
            return None
        if period and not 1 <= hour <= 12:
            return None
        if period in ("下午", "晚上") and hour < 12:
            hour += 12
        elif period in ("凌晨", "早上", "上午") and hour == 12:
            hour = 0
        elif period == "中午" and hour not in (11, 12, 1):
            return None
        elif period == "中午" and hour == 1:
            hour = 13
        minute_text = match["minute"] or "0"
        minute = 30 if minute_text == "半" else number(minute_text.removesuffix("分"))
        due = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        # Reject invalid/nonexistent civil times instead of guessing across DST.
        if due.astimezone(ZoneInfo("UTC")).astimezone(zone).replace(fold=0) != due.replace(fold=0):
            return None
        if due.replace(fold=0).utcoffset() != due.replace(fold=1).utcoffset():
            return None
    except (KeyError, ValueError, ZoneInfoNotFoundError):
        return None
    title = text[match.end():].strip(" ，,。！!")
    for prefix in ("提醒我", "记得", "要", "会"):
        if title.startswith(prefix):
            title = title[len(prefix):].strip()
            break
    if not title or title.startswith(("他", "她", "你", "我们", "老师", "同事", "老板", "朋友", "别人", "大家")):
        return None
    return title, due, match.group()
