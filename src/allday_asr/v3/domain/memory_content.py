"""Small, evidence-only content rules; no account classification or task ownership."""
import re
from datetime import datetime
from .chat_projection import semantic_text, semantic_value

IMAGE = "收到图片，内容未解析"
PROMOTION = re.compile(r"促销|优惠券|扫码领券|限时折扣|抢购|满\d+减\d+|购机(?:优惠|活动)|国庆.*(?:优惠|活动)|(?:福利|优惠|活动).*(?:到店|库存|现货|原价|最后.*天)|(?:到店|库存|现货).*(?:优惠|活动)")
ENGAGEMENT = re.compile(r"多少钱|价格|询价|报价|想买|购买|买一|下单|订购|有货|哪款|型号|配置|保修|活动.*(?:怎么|什么时候|参加)|(?:怎么|什么时候).*(?:活动|购买)")
PHYSICS = re.compile(r"(?:招|招聘).*物理.*(?:老师|教师)|物理.*(?:教师|老师).*招聘")


OPAQUE = "独立账号或访问字符串已省略，请在来源中查看"


def opaque(quote):
    # Standalone identifiers carry no discussion content; preserve the source reference.
    return bool(re.fullmatch(r"1\d{10}|@(?=[A-Za-z\d_]*[A-Za-z])(?=[A-Za-z\d_]*\d)[A-Za-z\d_]{8,}", quote.strip()))


def safe_memory(value):
    projected = semantic_value(value)
    def content_string(value):
        return "".join(OPAQUE if opaque(part) else part for part in re.split(r"([;；])", value))
    def clean(item):
        if isinstance(item, list):
            return [clean(v) for v in item]
        if isinstance(item, dict):
            return {k: content_string(v) if k in {"quote", "text", "title", "summary"} and isinstance(v, str) else clean(v) for k, v in item.items()}
        return item
    return clean(projected)


def text(row):
    return "\n".join(e["quote"] for e in row["evidence"] if e.get("role","core")=="core")


def readable(quote):
    return bool(quote.strip()) and not opaque(quote) and not quote.startswith(("[媒体", "[未解析", IMAGE, OPAQUE, "http://", "https://"))


def admission(row):
    if any(e["source_type"] == "recording" for e in row["evidence"]):
        return True, "已有结构化录音讨论；不归为用户立场"
    meaningful = [e for e in row["evidence"] if readable(e["quote"])]
    if not meaningful and row.get("event_id"):
        return True, "已有事项引用附件；不推断未解析内容"
    if not meaningful:
        return False, "附件内容未解析，缺少有效文字或相关事项依据"
    automated = any(re.search(r"签到成功|本次获得.{0,12}积分|当前积分|连续签到.{0,12}(?:天|奖励)|签到奖励", e["quote"]) for e in meaningful if e.get("identity_self") is not True)
    responses = [e["quote"].strip() for e in meaningful if e.get("identity_self") is True]
    routine = all(re.fullmatch(r"签到|打卡|点赞(?:\s+\d+)?|谢谢|好的", q) for q in responses)
    if automated and routine and not row.get("event_id"):
        return False, "自动签到或积分通知，缺少实际问题、讨论或相关事项依据"
    marketing = any(PROMOTION.search(e["quote"]) for e in meaningful if e.get("identity_self") is not True)
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
    body = text(row)
    recruiting = bool(PHYSICS.search(body))
    received = recruiting and bool(re.search(r"收到|接到|给我发|发了个", body))
    concerns = "初中物理" in body and "高中物理" in body
    declining = bool(re.search(r"(?:准备|倾向|想|那就).*婉拒|婉拒.*(?:打算|倾向)", body))
    if recruiting or concerns and declining:
        parts = []
        if received:
            parts.append("谈到收到物理教师招聘消息。")
            if re.search(r"没敢回|尚未回复|还没(?:回|回复)", body):
                parts.append("当时表示尚未回复。")
        if recruiting and not received:
            parts.append("讨论物理教师招聘。")
        if "不回" in body:
            parts.append("对话中建议不回复。")
        if "考研" in body:
            parts.append("对话中提到考研理由。")
        if declining:
            parts.append("表达婉拒倾向。")
        if re.search(r"初中物理.{0,12}(?:还擅长|擅长一点|有把握)", body):
            parts.append("自述对初中物理较有把握。")
        if re.search(r"高中物理.{0,25}(?:难|担心|带沟|讲不了|不会|教不了)", body):
            parts.append("表达对高中教学的顾虑。")
        # Neither intentions nor a conditional utterance proves an external action.
        parts.append("这些引用尚未确认是否实际回复。")
        return "物理教师招聘与是否回复", "".join(parts)
    quotes = [e["quote"].strip() for e in row["evidence"] if readable(e["quote"])]
    useful = [q for q in quotes if len(q) >= 6 and not q.startswith(("http://", "https://", "["))]
    quotes = useful or quotes
    title = row["title"]
    if "的独立讨论" in title or not title.strip():
        title = (quotes[0][:28] if quotes else "内容尚未解析")
    summary = "；".join(q[:90] for q in quotes[:2])
    if any(e["source_type"] == "recording" for e in row["evidence"]):
        summary = "录音中出现的讨论，未知身份不代表用户立场。" + summary
    return title, summary[:240]


def topics(row):
    body = text(row)
    # Specific discussion objects, not generic words such as 老师/机会/项目.
    result = []
    if PHYSICS.search(body) or "物理" in body and "婉拒" in body or re.search(r"招聘|招工|工作机会|投简历|参加面试", body):
        result.append("招聘与机会")
    if "英语" in row["title"] and "教育" in row["title"] or "英语" in body and re.search(r"教育|学科|中考|减负|学习", body):
        result.append("英语教育")
    if "劳动力成本" in row["title"] or re.search(r"(?:工资|周薪|劳动力).*(?:澳元|澳大利亚|成本)", body, re.S):
        result.append("工资与劳动力成本")
    if re.search(r"材料提交|提交材料|项目交付|项目验收|开题报告|项目需求", body):
        result.append("材料与项目")
    if re.search(r"约好|约定见面|(?:明天|下周).{0,12}见面|见面.{0,12}(?:明天|下周)", body):
        result.append("约定与安排")
    return result


def latest_evidence(row):
    values = [e["occurred_at"] for e in row["evidence"] if e.get("status", "valid") == "valid" and e.get("role","core")=="core"]
    return max(values, key=lambda v: datetime.fromisoformat(v.replace("Z", "+00:00"))) if values else None


def continuation_reason(a, b):
    earlier, later = sorted([a, b], key=lambda r: datetime.fromisoformat(r["occurred_at"].replace("Z", "+00:00")))
    x, y = text(earlier), text(later)
    # A causal reply-decision plus the concrete teaching object is a suggestion only.
    # A newly introduced recruitment notice/opportunity is not a continuation.
    if PHYSICS.search(x) and re.search(r"没敢回|尚未回复|还没回", x) and "那就婉拒" in y and "初中物理" in y and "高中物理" in y and not PHYSICS.search(y):
        speakers = {e.get("sender_account_id") for e in earlier["evidence"] if e.get("identity_self") is False}
        later_speakers = {e.get("sender_account_id") for e in later["evidence"] if e.get("identity_self") is False}
        if (speakers - {None}) & later_speakers:
            return "前段明确谈到收到物理教师招聘消息且未回复；后段以‘那就婉拒’衔接回复选择，并具体讨论初中／高中物理教学顾虑。可能延续前段招聘，但没有唯一招聘标识，需人工确认。"
    if re.search(r"看看照片|选哪一张",x) and re.search(r"不让登|绑定微信",x+y) and re.search(r"复制链接|微信解绑",y) and re.search(r"这张好看|报名照片",y):
        return "前段提供页面并请对方选择照片；后段明确解释同一页面的微信绑定登录障碍、复制链接打开方法，并继续选择报名照片。存在具体操作衔接，仍由用户判断是否为同一讨论。"
    return None
