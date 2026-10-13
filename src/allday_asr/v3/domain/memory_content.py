"""Small, evidence-only content rules; no account classification or task ownership."""

import re
from datetime import datetime
from .chat_projection import semantic_value
from .recall_language import shared_anchor, common_object_id

IMAGE = "收到图片，内容未解析"
PROMOTION = re.compile(
    r"促销|优惠券|扫码领券|限时折扣|抢购|满\d+减\d+|(?:福利|优惠|活动).*(?:到店|库存|现货|原价|最后.*天)|(?:到店|库存|现货).*(?:优惠|活动)"
)
ENGAGEMENT = re.compile(
    r"多少钱|价格|询价|报价|想买|购买|买一|下单|订购|有货|哪款|型号|配置|保修|活动.*(?:怎么|什么时候|参加)|(?:怎么|什么时候).*(?:活动|购买)"
)


OPAQUE = "独立账号或访问字符串已省略，请在来源中查看"


def opaque(quote):
    # Standalone identifiers carry no discussion content; preserve the source reference.
    return bool(
        re.fullmatch(
            r"1\d{10}|@(?=[A-Za-z\d_]*[A-Za-z])(?=[A-Za-z\d_]*\d)[A-Za-z\d_]{8,}",
            quote.strip(),
        )
    )


def safe_memory(value):
    projected = semantic_value(value)

    def content_string(value):
        return "".join(
            OPAQUE if opaque(part) else part for part in re.split(r"([;；])", value)
        )

    def clean(item):
        if isinstance(item, list):
            return [clean(v) for v in item]
        if isinstance(item, dict):
            return {
                k: content_string(v)
                if k in {"quote", "text", "title", "summary"} and isinstance(v, str)
                else clean(v)
                for k, v in item.items()
            }
        return item

    return clean(projected)


def text(row):
    return "\n".join(
        e["quote"] for e in row["evidence"] if e.get("role", "core") == "core"
    )


def readable(quote):
    return (
        bool(quote.strip())
        and not opaque(quote)
        and not quote.startswith(
            ("[媒体", "[未解析", IMAGE, OPAQUE, "http://", "https://")
        )
    )


def admission(row):
    if any(e["source_type"] == "recording" for e in row["evidence"]):
        return True, "已有结构化录音讨论；不归为用户立场"
    meaningful = [e for e in row["evidence"] if readable(e["quote"])]
    if not meaningful and row.get("event_id"):
        return True, "已有事项引用附件；不推断未解析内容"
    if not meaningful:
        return False, "附件内容未解析，缺少有效文字或相关事项依据"
    automated = any(
        re.search(
            r"签到成功|本次获得.{0,12}积分|当前积分|连续签到.{0,12}(?:天|奖励)|签到奖励",
            e["quote"],
        )
        for e in meaningful
        if e.get("identity_self") is not True
    )
    responses = [
        e["quote"].strip() for e in meaningful if e.get("identity_self") is True
    ]
    routine = all(
        re.fullmatch(r"签到|打卡|点赞(?:\s+\d+)?|谢谢|好的", q) for q in responses
    )
    if automated and routine and not row.get("event_id"):
        return False, "自动签到或积分通知，缺少实际问题、讨论或相关事项依据"
    marketing = any(
        PROMOTION.search(e["quote"])
        for e in meaningful
        if e.get("identity_self") is not True
    )
    if marketing:
        responses = [e for e in meaningful if e.get("identity_self") is True]
        engaged = any(ENGAGEMENT.search(e["quote"]) for e in responses)
        # An actual existing follow-up is evidence independent of marketing wording.
        if not engaged and not row.get("event_id"):
            return False, "促销消息缺少本人询价、购买、活动关注或相关事项依据"
        return True, "本人相关回应或已有事项依据"
    substantial = [e for e in meaningful if len(e["quote"].strip()) >= 6]
    if not substantial and not row.get("event_id"):
        return False, "一般短消息，尚无值得记住的讨论内容"
    return True, "有实际讨论内容；不要求产生本人待办"


def content(row):
    # Excerpts are the deterministic fallback. Never turn a lexical hit into a
    # paraphrased receipt, intention, ability, commitment or completed action.
    quotes = [e["quote"].strip() for e in row["evidence"] if readable(e["quote"])]
    selected = list(dict.fromkeys(quotes[:1] + quotes[-2:]))
    title = "原文：" + (quotes[0][:28] if quotes else "内容尚未解析")
    summary = "原文摘录（不推断事实状态）：" + "；".join(
        "「" + q[:160] + ("…[片段截断]" if len(q) > 160 else "") + "」"
        for q in selected
    )
    if any(e["source_type"] == "recording" for e in row["evidence"]):
        summary = "录音发言不代表本人执行。" + summary
    return title, summary


def topics(row):
    # Source-grounded labels replace story-specific subject classifications.
    # Shared vocabulary may suggest a topic; it never proves a shared business object.
    quotes = [e["quote"].strip() for e in row["evidence"] if readable(e["quote"])]
    if not quotes:
        return []
    anchor = shared_anchor(quotes[0], quotes[-1]) if len(quotes) > 1 else ""
    return [anchor or quotes[0][:28]]


def latest_evidence(row):
    values = [
        e["occurred_at"]
        for e in row["evidence"]
        if e.get("status", "valid") == "valid" and e.get("role", "core") == "core"
    ]
    return (
        max(values, key=lambda v: datetime.fromisoformat(v.replace("Z", "+00:00")))
        if values
        else None
    )


def continuation_reason(a, b):
    earlier, later = sorted([a, b], key=lambda r: r["occurred_at"])

    def stamp(r):
        return datetime.fromisoformat(r["occurred_at"].replace("Z", "+00:00"))

    if (stamp(later) - stamp(earlier)).total_seconds() > 3 * 86400:
        return None
    speakers = {
        e.get("sender_account_id")
        for e in earlier["evidence"]
        if e.get("identity_self") is not True
    } - {None}
    other = {
        e.get("sender_account_id")
        for e in later["evidence"]
        if e.get("identity_self") is not True
    } - {None}
    if not speakers & other:
        return None
    anchor = common_object_id(text(earlier), text(later))
    if anchor:
        return (
            "同一会话参与者的相邻记录具有相同显式对象编号「"
            + anchor
            + "」；仅为连续性候选，是否同一事情仍需人工确认。"
        )
    return None
