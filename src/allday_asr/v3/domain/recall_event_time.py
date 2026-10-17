"""Source-relative dates; query scope parsing remains deliberately stricter."""

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from .recall_query import DATE, RELATIVE, dates


FULL_DATE = re.compile(
    r"(?<!\d)(?:(\d{4})-(\d{1,2})-(\d{1,2})|"
    r"(\d{4})/(\d{1,2})/(\d{1,2})|"
    r"(\d{4})年(\d{1,2})月(\d{1,2})日)(?!\d)"
)


def full_explicit_dates(text):
    """Calendar dates actually written with a year in original text, not metadata."""
    result = set()
    for match in FULL_DATE.finditer(text):
        parts = [int(v) for v in match.groups() if v is not None]
        try:
            result.add(date(*parts).isoformat())
        except ValueError:
            continue
    return sorted(result)


def source_relative_period(quote, timezone, sent_at):
    future = re.findall(r"明天晚上|今天晚上|明晚|今晚|明天|后天", quote)
    if future:
        if (
            len(future) != 1
            or RELATIVE.search(
                re.sub(r"明天晚上|今天晚上|明晚|今晚|明天|后天", "", quote)
            )
            or re.search(DATE, quote)
            or re.search(r"或|还是", quote)
        ):
            raise ValueError("ambiguous source-relative dates")
        zone = ZoneInfo(timezone)
        day = datetime.fromtimestamp(sent_at, zone).date() + timedelta(
            days={
                "明晚": 1,
                "明天": 1,
                "明天晚上": 1,
                "后天": 2,
                "今晚": 0,
                "今天晚上": 0,
            }[future[0]]
        )
        first = datetime.combine(day, datetime.min.time(), zone)
        return int(first.timestamp()), int((first + timedelta(days=1)).timestamp()) - 1
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


def validate_message_dayparts(text, hour, source_quotes=()):
    """Check asserted utterance periods, allowing a narrowly negated comparison.

    Negating a daypart does not assert it. An affirmative correction after 而是
    starts a new clause and must still agree with the cited utterance.
    """
    spans = {
        "凌晨": (0, 6),
        "早上": (6, 12),
        "上午": (0, 12),
        "中午": (11, 14),
        "下午": (12, 18),
        "晚上": (18, 24),
    }
    for clause in re.split(r"[，,；;。：:]|而是", text):
        for match in re.finditer("|".join(spans), clause):
            before = clause[: match.start()]
            negated = re.search(
                r"(?:并非|并不是|不是|而非|而不是|尚不能确认|不能确认|无法确认|没有证据表明)"
                r"(?:在|发生于|发生在|前天|昨天|今天|昨|\d{1,2}月\d{1,2}[日号]?|\d{1,2}[日号]|[（）()\s])*$",
                before,
            )
            if negated:
                continue
            # In a dated comparison, this period belongs to the comparison's
            # object, not to the cited utterance. Check each remaining period
            # independently; a later affirmative correction is still checked.
            comparison = re.search(
                r"比(?:你(?:所)?说的|所问的|预期的|原问题中的)?"
                r"(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|"
                r"(?:\d{4}年)?\d{1,2}月\d{1,2}[日号]?|"
                r"\d{1,2}[日号]|前天|昨天|今天|明天|后天)的?$",
                before,
            )
            if comparison and re.match(r"(?:更)?[早晚]", clause[match.end() :]):
                continue
            # A reported previous state or proposed later action is not the
            # timestamp of the utterance reporting it. This exception requires
            # the same period in a cited original and a grammatical attribution
            # or modality; affirmative message-time assertions remain checked.
            word = match.group()
            supported = any(word in quote for quote in source_quotes)
            after = clause[match.end() :]
            explicit_before = re.search(
                r"(?:消息|记录|发言|讨论)(?:实际)?(?:发生于|发生在|在|于)?"
                r"(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|(?:\d{4}年)?\d{1,2}月\d{1,2}[日号]?|\d{1,2}[日号]|今天|昨天|前天|的|\s)*$",
                before,
            )
            speech_after = re.search(
                r"^[”’\"]?(?:是.{0,12})?(?:.{0,12}?)(?:说|问|发言|发送|发出|回复|写道|表示|提到|讨论|聊|谈|交流|沟通)",
                after,
            )
            modal = re.search(
                r"(?:建议|打算|准备|计划|希望|约好|想|等待|等)[^，,；;。：:]{0,12}$",
                before,
            )
            modal = modal or re.match(r"(?:再|会|准备|打算|计划)", after)
            # A cited temporal proposition may be paraphrased without repeating
            # "said". Reject explicit utterance-time predicates; do not treat
            # supported state/plan content as the sending time of that report.
            if supported and not explicit_before and (modal or not speech_after):
                continue
            a, b = spans[word]
            if not a <= hour < b:
                raise ValueError("message daypart disagrees with cited utterance")
