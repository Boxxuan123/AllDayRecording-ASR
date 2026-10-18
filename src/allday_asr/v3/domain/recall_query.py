"""Deterministic constraints from confirmed profiles; no model or scope relaxation."""

import re
from datetime import datetime, timedelta, time as clock_time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .recall_language import plan
from .recall_subject_span import address_span, address_context, group_topic_span, known_label
from .recall_investigation_scope import platform_allowed

DATE = r"(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|(?:\d{4}年)?\d{1,2}月\d{1,2}[日号])"
RELATIVE = re.compile(r"昨天晚上|昨晚|大前天|前天|昨天|今天|上周|本周|这周")
RELATION = re.compile(
    r"(?:我们|我|本人)\s*(?:在\s*)?(?:跟|和|与)\s*(.+?)\s*"
    r"(?=(?:(?:这|那)(?:段|次)|的)?(?:私聊|聊天|对话)|聊|谈|讨论|交流|沟通|说|后来|之后|后续)"
    r"|(.+?)(?:跟|和|与)(?:我们|我|本人)(?=聊|谈|讨论|交流|沟通|说)"
)
RECAP = r"(?:说了|说过|发了|发过|聊了|聊过|谈了|谈过|讨论了|讨论过|交流了|交流过)(?:什么|啥)"
SPEAKER = re.compile(
    r"(.+?)(?=" + RECAP + r"|的发言|的原话|说的[“「\"‘]|在.{1,64}" + RECAP + r")"
)
ROOM_PREFIX = re.compile(
    r"^(?:在\s*)?(?:[“「\"‘].{1,100}?[”」\"’]|[^，,；;:：]{1,100}?)"
    r"\s*群(?:聊)?(?:里|中|内)\s*"
)


def speaker_span(value, people):
    """Keep an explicit room heading as context, never an inferred person name."""
    value = value.strip(" ，。？?")
    labels = (
        n.strip().casefold()
        for person in people.values()
        for n in [person["display_name"], *person.get("aliases", [])]
        if isinstance(n, str)
    )
    # A supported full native label may itself contain grammatical room words.
    if value.casefold() in labels:
        return value, ""
    prefix = ROOM_PREFIX.match(value)
    if prefix and prefix.end() < len(value):
        return value[prefix.end():].strip(" ，。？?"), value[:prefix.end()]
    return value, ""

def name_in_text(name, text):
    """A native nickname must not capture part of a different identifier."""
    if re.fullmatch(r"[A-Za-z0-9_]+", name):
        return bool(
            re.search(
                r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", text, re.I
            )
        )
    return len(name) > 1 and re.sub(r"\s+", "", name).casefold() in re.sub(r"\s+", "", text).casefold()



TIME_PRECISION_NOTE = (
    "检索按日期端点覆盖，深夜侧从18:00起、刚过零点侧至06:00前作为候选窗口；"
    "这是口语时段的宽检索边界，引用消息的实际时刻另行核对，"
    "不能据此确认精确事件起止。"
)


def short_day_tail(question, first_date):
    """Inherit only an explicitly written month/year, never guess a rollover."""
    tail = question[first_date.end():]
    match = re.match(
        r"\s*(?P<before>深夜|午夜)?\s*(?:到|至|~|～|—|–|-)\s*"
        r"(?P<day>\d{1,2})[日号](?P<after>\s*(?:刚过零点|刚过午夜|零点前后))?",
        tail,
    )
    if match:
        if re.match(r"\s*(?:凌晨|早上|上午|中午|下午|晚上|零点|\d{1,2}:)", tail[match.end():]):
            raise ValueError("时间描述尚不支持该跨日时刻组合，请明确起止时间")
        return match
    if re.search(r"(?:到|至|~|～|—|–|-)\s*\d{1,2}[日号]", tail):
        raise ValueError("时间描述尚不支持该短日端点的时段，请明确起止时间")
    return None


def dates(question, timezone, now):
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError("用户时区无效，请使用 IANA 时区") from None
    if re.search(
        r"上个月|去年|最近\d+天|昨夜|昨儿|前晚|今晚|近日|近来|刚才|前阵子|那天|某天|几天前|近期|夜里|下班后|明天|后天|下周|[一二三四五六七八九十两]{1,3}点|\d{1,2}点(?:半|\d{1,2}分)?",
        question,
    ):
        raise ValueError("时间描述尚不支持，请使用明确日期或昨晚；本轮未检索")
    today = datetime.fromtimestamp(now, zone).date()
    matches = list(re.finditer(DATE, question))
    relative = list(RELATIVE.finditer(question))
    if (matches and relative) or len(relative) > 1 or len(matches) > 2:
        raise ValueError("时间描述有多个范围，请明确日期")
    labels = []
    short_tail = short_day_tail(question, matches[0]) if len(matches) == 1 else None
    if matches:
        try:

            def calendar_date(match):
                parts = list(map(int, re.findall(r"\d+", match.group())))
                return datetime(
                    *(parts if len(parts) == 3 else [today.year, *parts])
                ).date()

            first, last = calendar_date(matches[0]), calendar_date(matches[-1])
            if short_tail:
                last = first.replace(day=int(short_tail["day"]))
        except ValueError:
            raise ValueError("日期无效") from None
        if len(matches) == 2 and not re.fullmatch(
            r"\s*(?:到|至|~|～|—|–)\s*", question[matches[0].end() : matches[1].start()]
        ):
            raise ValueError("请明确日期范围的起止关系")
        labels = [m.group() for m in matches]
        if short_tail:
            labels.append(short_tail.group())
    elif relative:
        label = relative[0].group()
        labels = [label]
        if label in {"昨晚", "昨天晚上"}:
            first = last = today - timedelta(days=1)
        elif label in {"上周", "本周", "这周"}:
            first = today - timedelta(
                days=today.weekday() + (7 if label == "上周" else 0)
            )
            last = first + timedelta(days=6)
        else:
            first = last = today - timedelta(
                days={"今天": 0, "昨天": 1, "前天": 2, "大前天": 3}[label]
            )
    else:
        if re.search(
            r"上个月|去年|最近\d+天|昨夜|昨儿|前晚|今晚|近日|近来|\d{1,2}:\d{2}",
            question,
        ):
            raise ValueError("请使用明确日期或日期范围描述时间；本轮未检索")
        return None, question
    if re.search(r"上个月|去年|最近\d+天|昨夜|昨儿|前晚|今晚|近日|近来", question):
        raise ValueError("时间描述尚不支持，请使用明确日期；本轮未检索")
    if last < first:
        raise ValueError("结束日期早于开始日期")
    start = datetime.combine(first, datetime.min.time(), zone)
    end = datetime.combine(last + timedelta(days=1), datetime.min.time(), zone)
    # Approximate night expressions use the existing broad 18:00--06:00
    # retrieval envelope, not two complete days or an asserted event time.
    if short_tail:
        if short_tail["before"]:
            start = datetime.combine(first, clock_time(18), zone)
        if short_tail["after"]:
            end = datetime.combine(last, clock_time(6), zone)
    # An adjacent date+clock identifies a message minute/second. A clock in an
    # event description later in the question is not silently a send-time filter.
    anchored_point = any(re.search(re.escape(label) + r"\s*\d{1,2}:\d{2}", question)
                         for label in labels)
    for label in labels:
        question = question.replace(label, " ")
    question = re.sub(r"^\s*(?:到|至|~|～|—|–)\s*", "", question)
    if labels and labels[0] in {"昨晚", "昨天晚上"} and not re.search(r"\d{1,2}:\d{2}", question):
        start = datetime.combine(first, clock_time(18), zone)
        end = datetime.combine(today, clock_time(6), zone)
        # A night still in progress is bounded by the query's actual instant.
        return (int(start.timestamp()), min(int(end.timestamp()) - 1, now)), question
    hours = re.search(
        r"(?<![\d:])(\d{1,2}):(\d{2})\s*(?:到|至|~|～|-)\s*(\d{1,2}):(\d{2})(?![\d:])", question
    )
    points = list(re.finditer(r"(?<![\d:])(\d{1,2}):(\d{2})(?::(\d{2}))?(?![\d:])", question))
    point = points[0] if not hours and anchored_point and len(points) == 1 else None
    if points and not hours and not point:
        raise ValueError("请明确起止时刻，不能忽略时间约束")
    if point and (re.search(r"(?:到|至|~|～|—|–|-)\s*$", question[:point.start()])
                  or re.match(r"\s*(?:到|至|~|～|—|–|-)", question[point.end():])):
        raise ValueError("请明确起止时刻，不能忽略时间约束")
    daytime = re.search(r"凌晨|早上|上午|中午|下午|晚上", question)
    if hours or daytime or point:
        if first != last or ((hours or point) and daytime):
            raise ValueError("请为时间段指定一个明确自然日")
        if hours:
            a, b, c, d = map(int, hours.groups())
            try:
                start = datetime.combine(first, clock_time(a, b), zone)
                end = datetime.combine(first, clock_time(c, d), zone)
            except ValueError:
                raise ValueError("时刻无效") from None
            question = question.replace(hours.group(), " ")
        elif point:
            hour, minute = int(point[1]), int(point[2])
            try:
                clock = clock_time(hour, minute, int(point[3] or 0))
            except ValueError:
                raise ValueError("时刻无效") from None
            point_day = first
            if labels[0] in {"昨晚", "昨天晚上"}:
                if hour < 6:
                    point_day += timedelta(days=1)
                elif hour < 18:
                    raise ValueError("指定时刻不在昨晚的时段内")
            start = datetime.combine(point_day, clock, zone)
            end = start + timedelta(seconds=1 if point[3] is not None else 60)
            question = question.replace(point.group(), " ", 1)
        else:
            a, b = {
                "凌晨": (0, 6),
                "早上": (6, 12),
                "上午": (0, 12),
                "中午": (11, 14),
                "下午": (12, 18),
                "晚上": (18, 24),
            }[daytime.group()]
            start = datetime.combine(first, clock_time(a), zone)
            end = datetime.combine(
                first + timedelta(days=b // 24), clock_time(b % 24), zone
            )
            question = question.replace(daytime.group(), " ")
        if end <= start:
            raise ValueError("结束时刻必须晚于开始时刻")
        for value in (start, end):
            if (
                datetime.fromtimestamp(value.timestamp(), zone).replace(tzinfo=None)
                != value.replace(tzinfo=None)
                or value.utcoffset() != value.replace(fold=1).utcoffset()
            ):
                raise ValueError("时刻位于时区切换边界，请指定更明确的时间")
    elif re.search(r"\d{1,2}:\d{2}", question):
        raise ValueError("请明确起止时刻，不能忽略时间约束")
    return (int(start.timestamp()), int(end.timestamp()) - 1), question


def named_platforms(question):
    return [p for p, label in (("qq", "QQ"), ("wechat", "微信"), ("recording", "录音")) if re.search(label, question, re.I)]


def understand(question, keyword, people, payload, now):
    timezone = payload.get("timezone", "Asia/Singapore")
    # An explicit literal lookup may use question == keyword plus source/time.
    # Their quoted names and relative dates describe the original message, not
    # new query constraints. Preserve the literal lookup constraints.
    literal = bool(keyword.strip()) and keyword.strip() == question.strip()
    period, remaining = dates("" if literal else question, timezone, now)
    scope = {
        "person_id": payload.get("person_id", ""),
        "source": payload.get("source", "all"),
        "sent_from": payload.get("sent_from", now - 30 * 86400),
        "sent_to": payload.get("sent_to", now),
        "timezone": timezone,
        "relationship": "speaker",
    }
    if literal:
        return {
            **plan(question, keyword),
            "intent": "quote",
            "scope": scope,
            "person_mention": "",
            "time_explicit": False,
            "issue": "",
            "person_candidates": [],
        }
    if period:
        date_matches = list(re.finditer(DATE, question))
        tail = short_day_tail(question, date_matches[0]) if len(date_matches) == 1 else None
        if tail and (tail["before"] or tail["after"]):
            scope["time_precision_note"] = TIME_PRECISION_NOTE
        scope["sent_from"] = max(period[0], payload.get("sent_from", period[0]))
        scope["sent_to"] = min(period[1], payload.get("sent_to", period[1]))
        if scope["sent_from"] > scope["sent_to"]:
            raise ValueError("问题日期与所选日期不相交，请明确范围")
    platforms = named_platforms(remaining)
    if len(platforms) == 1:
        if scope["source"] not in {"all", platforms[0]}:
            raise ValueError("问题来源与所选来源冲突")
        scope["source"] = platforms[0]
    elif len(platforms) > 1:
        if scope["source"] != "all":
            raise ValueError("问题来源与所选来源冲突")
        scope["source_platforms"] = platforms
    remaining = re.sub(r"QQ|微信|录音", " ", remaining, flags=re.I)
    relation = RELATION.search(remaining.strip())
    speaker = SPEAKER.search(remaining.strip(" ，。？?")) if not relation else None
    if speaker and re.fullmatch(r"(?:群聊|群(?:里|中|内))(?:的)?(?:大家|成员|消息)?", speaker.group().strip()):
        speaker = None
    mention = ""
    room_mention = ""
    if relation:
        mention = next(v for v in relation.groups() if v).strip(" ，。？?")
        scope["relationship"] = "conversation"
        normalized = address_span(mention, people)
        topics = [normalized] if known_label(normalized, people) else re.split(r"关于|有关", mention, maxsplit=1)
        mention, modifier = address_context(topics[0].strip(), people)
        remaining = modifier + " " + remaining.replace(relation.group(), " ", 1)
        if len(topics) > 1:
            remaining = topics[1] + " " + remaining
    elif speaker:
        group_topic = group_topic_span(speaker.group(), people)
        if group_topic:
            room_mention, topic = group_topic
            scope["relationship"] = "group"
            remaining = topic + " " + remaining.replace(speaker.group(), " ", 1)
        else:
            mention, room_context = speaker_span(speaker.group(), people)
            mention = address_span(mention, people)
            if room_context:
                room_mention = re.sub(r"^在\s*|\s*群(?:聊)?(?:里|中|内)\s*$", "", room_context).strip(" “」「\"‘’”")
            remaining = remaining.replace(speaker.group(), " ", 1)
    elif re.search(r"群聊|群里|群中", remaining):
        scope["relationship"] = "group"
    names = {}
    for pid, person in people.items():
        for name in [person["display_name"], *person.get("aliases", [])]:
            if person.get("platform") and not platform_allowed(scope, person["platform"]):
                continue
            if isinstance(name, str) and name.strip() and person["kind"] != "self":
                names.setdefault(name.strip().casefold(), []).append(pid)
    multiple_people = False
    if not mention and not room_mention:
        found = [n for n in names if name_in_text(n, remaining)]
        multiple_people = (
            len({p for n in found for p in names[n]}) > 1 and len(found) > 1
        )
        if found:
            mention = max(found, key=len)
            remaining = re.sub(r"\s*".join(re.escape(p) for p in mention.split()), " ", remaining, flags=re.I)
    candidates = (
        list(dict.fromkeys(names.get(mention.casefold(), []))) if mention else []
    )
    issue = (
        "问题涉及多个不同人物，请明确本轮交流对象；本轮未检索。"
        if multiple_people
        else ""
    )
    if mention:
        if not candidates:
            issue = (
                f"“{mention}”没有可信的已确认姓名或别名映射，请先确认人物；本轮未检索。"
            )
        elif scope["person_id"]:
            if scope["person_id"] not in candidates:
                issue = "问题人物与所选人物不一致，请重新选择；本轮未检索。"
        elif len(candidates) > 1:
            issue = f"“{mention}”对应多个已确认人物，请选择具体人物后重试；本轮未检索。"
        else:
            scope["person_id"] = candidates[0]
    if scope["person_id"] and scope["person_id"] not in people:
        issue = "指定人物尚未确认；本轮未检索，也未回退到全部人物。"
    query = plan(remaining, keyword)
    query["intent"] = plan(question, keyword)["intent"]
    if (
        scope["relationship"] == "conversation"
        and (query["intent"] == "quote" or (query["intent"] == "question" and not query["terms"]))
        and not re.search(r"原话|原文|逐字", question)
    ):
        query["intent"] = "conversation"
    if scope["relationship"] == "conversation" and not scope["person_id"]:
        issue = issue or "请确认交流对象；本轮未检索。"
    if (
        not query["terms"]
        and not scope["person_id"]
        and scope["relationship"] != "group"
    ):
        issue = issue or "请明确人物或聊天主题；本轮未检索。"
    return {
        **query,
        "scope": scope,
        "person_mention": mention,
        "room_mention": room_mention,
        "time_explicit": period is not None,
        "message_time_explicit": bool(period and re.search(r"\d{1,2}:\d{2}", question)),
        "issue": issue,
        "person_candidates": [people[p] for p in candidates],
    }
