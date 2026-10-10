"""Evidence and time validation for shared chat-sourced events."""

from datetime import datetime, timedelta
from hashlib import sha256
import json
import re
from zoneinfo import ZoneInfo

from .chat_data import packed
from .reminder_time import number
from .chat_projection import semantic_record, semantic_text, semantic_value
from allday_asr.v3.ports.chat_data import ChatDataError

VERSION = "chat-followup-v1"


def conversation_key(record):
    return packed(
        [record["platform"], record["source_account_id"], record["conversation_id"]]
    )


def evidence_key(dataset, record):
    return sha256(
        packed(
            [
                dataset,
                conversation_key(record),
                record["record_id"],
                record["record_revision"],
            ]
        ).encode()
    ).hexdigest()


def actor_key(record, account):
    if not account:
        return "unknown"
    return packed([record["platform"], record["source_account_id"], account])


def resolve_time(expression, anchor, timezone_name, due_date, due_at):
    if not expression:
        if due_date or due_at:
            raise ChatDataError("UNSUPPORTED_FOLLOWUP_TIME", 422)
        return {
            "expression": "",
            "date": None,
            "at": None,
            "precision": "unknown",
            "timezone": timezone_name,
            "uncertainty": "没有明确时间",
        }
    if anchor.get("sent_at") is None or not timezone_name:
        return {
            "expression": expression,
            "date": None,
            "at": None,
            "precision": "unknown",
            "timezone": timezone_name,
            "uncertainty": "发送时间或时区未知",
        }
    try:
        zone = ZoneInfo(timezone_name)
    except Exception:
        raise ChatDataError("INVALID_FOLLOWUP_TIMEZONE", 422) from None
    sent = datetime.fromtimestamp(anchor["sent_at"], zone)
    day = None
    for word, delta in (("后天", 2), ("明天", 1), ("今天", 0), ("今晚", 0)):
        if word in expression:
            day = sent.date() + timedelta(days=delta)
            break
    weekday = re.search(r"(下周|本周|周|星期)([一二三四五六日天])", expression)
    if day is None and weekday:
        w = "一二三四五六日".find(weekday[2].replace("天", "日"))
        delta = w - sent.weekday()
        if weekday[1] == "下周":
            delta += 7
        elif weekday[1] in {"周", "星期"} and delta < 0:
            delta += 7
        day = sent.date() + timedelta(days=delta)
    explicit = re.search(r"(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})", expression)
    if explicit:
        try:
            day = datetime(*map(int, explicit.groups())).date()
        except ValueError:
            raise ChatDataError("INVALID_FOLLOWUP_TIME", 422) from None
    if day is None:
        return {
            "expression": expression,
            "date": None,
            "at": None,
            "precision": "unknown",
            "timezone": timezone_name,
            "uncertainty": "时间表达超出确定性解析范围",
        }
    if due_date and due_date != day.isoformat():
        raise ChatDataError("INVALID_FOLLOWUP_TIME", 422)
    at = None
    if due_at:
        clock = re.search(
            r"([0-9零〇一二两三四五六七八九十]{1,3})(?:[:：]([0-9零〇一二两三四五六七八九十]{1,3})|点(?:([0-9零〇一二两三四五六七八九十]{1,3})分|半)?|时)",
            expression,
        )
        if not clock:
            raise ChatDataError("INVENTED_FOLLOWUP_HOUR", 422)
        hour = number(clock[1])
        minute = number(clock[2] or clock[3] or ("三十" if "半" in clock[0] else "0"))
        if any(w in expression for w in ("下午", "晚上")) and hour < 12:
            hour += 12
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ChatDataError("INVALID_FOLLOWUP_TIME", 422)
        if (
            hour <= 12
            and not any(
                w in expression
                for w in ("上午", "凌晨", "下午", "晚上", "中午", "早上")
            )
            and ":" not in clock[0]
            and "：" not in clock[0]
        ):
            return {
                "expression": expression,
                "date": day.isoformat(),
                "at": None,
                "precision": "day",
                "timezone": timezone_name,
                "uncertainty": "小时的上午/下午含义未明确",
            }
        if any(w in expression for w in ("上午", "凌晨", "早上")) and hour == 12:
            hour = 0
        local = datetime(day.year, day.month, day.day, hour, minute)
        early, late = (
            local.replace(tzinfo=zone, fold=0),
            local.replace(tzinfo=zone, fold=1),
        )
        if early.utcoffset() != late.utcoffset():
            return {
                "expression": expression,
                "date": day.isoformat(),
                "at": None,
                "precision": "day",
                "timezone": timezone_name,
                "uncertainty": "夏令时切换存在歧义",
            }
        proposed = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
        if proposed.tzinfo is None or proposed != early:
            raise ChatDataError("INVALID_FOLLOWUP_TIME", 422)
        at = early.isoformat()
    return {
        "expression": expression,
        "date": day.isoformat(),
        "at": at,
        "precision": "minute" if at else "day",
        "timezone": timezone_name,
        "uncertainty": None,
    }


def validate_candidate(value, records, dataset, links, now_seconds, timezone_name=None):
    if not isinstance(value, dict) or value.get("action") not in {
        "create",
        "update",
        "cancel",
        "complete",
    }:
        raise ChatDataError("INVALID_FOLLOWUP_CANDIDATE", 422)
    if value.get("commitment") not in {
        "explicit",
        "proposal",
        "conditional",
        "negated",
        "reported",
        "unclear",
    }:
        raise ChatDataError("INVALID_FOLLOWUP_CANDIDATE", 422)
    title = value.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 300:
        raise ChatDataError("INVALID_FOLLOWUP_TITLE", 422)
    by_id = {r["record_id"]: r for r in records}
    refs = value.get("evidence")
    if not isinstance(refs, list) or not refs or len(refs) > 12:
        raise ChatDataError("INVALID_FOLLOWUP_EVIDENCE", 422)
    evidence = []
    for ref in refs:
        if not isinstance(ref, dict) or ref.get("record_id") not in by_id:
            raise ChatDataError("INVALID_FOLLOWUP_EVIDENCE", 422)
        r = by_id[ref["record_id"]]
        quote = ref.get("quote")
        if (
            not isinstance(quote, str)
            or not quote.strip()
            or (
                quote not in (r.get("text") or "")
                and quote not in semantic_text(r.get("text") or "")
            )
        ):
            raise ChatDataError("INVALID_FOLLOWUP_QUOTE", 422)
        evidence.append(
            {
                **semantic_record(r),
                "quote": semantic_text(quote),
                "source_quote": semantic_value(r.get("quote")),
                "raw_text_sha256": sha256((r.get("text") or "").encode()).hexdigest(),
                "dataset": dataset,
                "evidence_key": evidence_key(dataset, r),
            }
        )
    if len({conversation_key(r) for r in evidence}) != 1:
        raise ChatDataError("FOLLOWUP_CONVERSATION_MISMATCH", 422)
    anchor = by_id.get(value.get("anchor_id"))
    if not anchor or anchor["record_id"] not in {r["record_id"] for r in evidence}:
        raise ChatDataError("INVALID_FOLLOWUP_ANCHOR", 422)
    issue_quote = value.get("issue_quote")
    if (
        not isinstance(issue_quote, str)
        or not issue_quote.strip()
        or (
            issue_quote not in (anchor.get("text") or "")
            and issue_quote not in semantic_text(anchor.get("text") or "")
        )
    ):
        raise ChatDataError("INVALID_FOLLOWUP_ISSUE", 422)
    source_key = sha256(
        (
            packed([conversation_key(anchor), anchor["record_id"]])
            + ":"
            + str(
                (
                    anchor["text"]
                    if issue_quote in anchor["text"]
                    else semantic_text(anchor["text"])
                ).index(issue_quote)
            )
            + ":"
            + issue_quote
        ).encode()
    ).hexdigest()
    actor = value.get("actor_account_id")
    identities = {r.get("sender_account_id") for r in evidence}
    # Actor must be witnessed as a sender, never inferred from source-account or nickname.
    known = actor is not None and actor in identities
    person = links.get(actor_key(anchor, actor)) if known else None
    ambiguous = (
        value["commitment"] != "explicit"
        or value.get("relationship") != "explicit"
        or not known
    )
    self_known = any(
        v == "self"
        and json.loads(k)[:2] == [anchor["platform"], anchor["source_account_id"]]
        for k, v in links.items()
    )
    category = (
        "pending"
        if ambiguous or (person is None and not self_known)
        else ("self" if person == "self" else "waiting")
    )
    if value["commitment"] in {"negated", "reported"}:
        category = "pending"
    if value["action"] in {"complete", "cancel", "update"} and all(
        r["quote"].strip() in {"好", "好的", "行", "收到", "好了", "完成了"}
        for r in evidence
    ):
        category = "pending"
    value = {
        **value,
        "proposed_timezone": value.get("timezone"),
        "timezone": timezone_name,
    }
    time_info = resolve_time(
        value.get("time_expression") or "",
        anchor,
        value.get("timezone"),
        value.get("due_date"),
        value.get("due_at"),
    )
    newest = max(
        (r["sent_at"] for r in evidence if r.get("sent_at") is not None), default=None
    )
    historical = newest is None or newest < now_seconds - 7 * 86400
    target = value.get("target_event_id")
    if value["action"] != "create" and (not target or ambiguous):
        category = "pending"
    recipients = value.get("recipient_account_ids", [])
    if not isinstance(recipients, list) or any(
        not isinstance(x, str) for x in recipients
    ):
        raise ChatDataError("INVALID_FOLLOWUP_ACTOR", 422)
    interpretation_key = sha256(
        packed([VERSION, links, timezone_name]).encode()
    ).hexdigest()
    return {
        **value,
        "interpretation_key": interpretation_key,
        "title": semantic_text(title.strip()),
        "issue_quote": semantic_text(issue_quote),
        "category": category,
        "historical": historical,
        "progress_unknown": historical,
        "actor_person_id": person or actor_key(anchor, actor if known else None),
        "source_key": source_key,
        "conversation_key": conversation_key(anchor),
        "dataset": dataset,
        "evidence": evidence,
        "latest_sent_at": newest,
        "time": time_info,
        "version": VERSION,
    }
