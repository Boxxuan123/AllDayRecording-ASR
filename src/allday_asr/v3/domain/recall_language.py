"""Language-shape hints, never factual conclusions or identity decisions."""

import re
from difflib import SequenceMatcher

# Complete discourse phrases are separators, never substrings removed from names.
DISCOURSE = re.compile(
    r"发了什么|发生了什么|发生什么|帮我回忆一下|帮我回忆|回忆一下|请帮我|当初|当时|之前|上次|来着|为什么|为何|哪些|是什么|什么|怎么样|如何|后来|最终|最后|分别在|讨论过|提到过|说过|关于|这个方向|这件事情|事情经过|的事情经过|是怎样的|[，。？！?；：、\s]+",
    re.I,
)
PIVOT = re.compile(r"换个话题|另外一件|另一个|另一项|另有|与此无关")
STATE_HINT = re.compile(
    r"取消|改期|延期|改为|不再|已经|已完成|决定|确定|作废|撤回|没|未|不|如果|建议|答应|承诺|转述|据说"
)
FUNCTION = {
    "为什么",
    "是什么",
    "怎么样",
    "如何",
    "之前",
    "当时",
    "后来",
    "最终",
    "最后",
    "已经",
    "没有",
    "这个",
    "那个",
    "我想",
    "我们",
    "可以",
    "一下",
    "的话",
    "所以",
    "但是",
    "准备",
    "讨论",
}


def intent(question):
    if re.search(r"最终|最后|最新|现在|取消了吗|完成了吗", question):
        return "latest"
    if re.search(r"为什么|为何|原因|考虑因素", question):
        return "reason"
    # Temporal follow-up + a status question, independent of subjects/products.
    if re.search(
        r"(?:后来|之后|后续|随后|后面).{0,12}(?:如何|怎样|怎么样|情况|进展|结果|回复|答复|决定|落实|处理|变化)",
        question,
    ):
        return "latest"
    if re.search(r"经过|过程|进展|来龙去脉|发生了什么", question):
        return "timeline"
    if re.search(
        r"聊了什么|聊过什么|谈了什么|讨论了什么|对话回顾|聊天内容|交流了什么|(?<!发)发了什么",
        question,
    ):
        return "conversation"
    if re.search(r"原话|原文|精确|逐字|包含|含有", question):
        return "quote"
    return (
        "question"
        if re.search(
            r"什么|啥|怎么|如何|哪个|哪位|何人|是谁|指谁|谁在|谁说|何时|打算|安排|计划|准备|是否|[?？]|吗$", question
        )
        else "quote"
    )


def plan(question, keyword=""):
    if keyword.strip():
        return {
            "intent": intent(question),
            "terms": [keyword.strip()],
            "remote_terms": [keyword.strip()],
            "exact": True,
        }
    # Strip grammatical operators as complete phrases; never generate n-grams.
    separators = re.compile(
        DISCOURSE.pattern
        + r"|聊了什么|聊过什么|谈了什么|讨论了什么|交流了什么|说了啥|说过啥|说了什么|说过什么|聊天内容|对话回顾|原话|原文|帮我找|请查找|整个|筹备|最终决定|决定|考虑因素|提到的|群聊|群里|群中|在|\bQQ\b|微信|录音",
        re.I,
    )
    chunks = [s.strip() for s in separators.split(question) if s.strip()]
    terms = []
    for chunk in chunks:
        chunk = re.sub(
            r"^(?:不考虑|不选|没选|选择|考虑|不|选|与|和|我跟|我和|我与)", "", chunk
        )
        chunk = re.sub(r"(?:的|了|吗|呢)$", "", chunk)
        if len(chunk) >= 2 and chunk not in FUNCTION and chunk not in terms:
            terms.append(chunk[:100])
    return {
        "intent": intent(question),
        "terms": terms[:8],
        "remote_terms": terms[:2],
        "exact": False,
    }


def relevance(text, terms):
    matches = [t for t in terms if t.casefold() in text.casefold()]
    return max((len(t) ** 2 for t in matches), default=0) + min(len(matches), 20) / 20


def object_ids(text):
    return set(re.findall(r"[A-Za-z]{1,16}[-_]\d+|(?:编号|单号)\s*[A-Za-z0-9-]+", text))


def common_object_id(a, b):
    """One explicit object reference on both sides, never a lexical noun match."""
    if PIVOT.search(b):
        return ""
    left, right = object_ids(a), object_ids(b)
    return next(iter(left)) if len(left) == 1 and left == right else ""


def shared_anchor(a, b):
    if PIVOT.search(b):
        return ""
    left, right = object_ids(a), object_ids(b)
    if left and right and not left & right:
        return ""
    # Vocabulary display only. This overlap cannot establish discussion continuity
    # or generate a same-event review card; use reply edges or common_object_id.
    matches = SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks()
    parts = [a[m.a : m.a + m.size].strip(" ，。；：?!") for m in matches]
    parts = [s for s in parts if len(s) >= 2 and s not in FUNCTION]
    if len(parts) < 2:
        parts = [s for s in parts if len(s) >= 3]
    return max(parts, key=len, default="")[:40]


def select_evidence(rows, query_intent, terms, limit=20):
    ordered = sorted(
        [r for r in rows if r.get("validity", "valid") == "valid"],
        key=lambda r: (r.get("sent_at") or 0, r["id"]),
    )
    if len(ordered) <= limit:
        return ordered
    selected = {}

    def add(r):
        if len(selected) < limit:
            selected[r["id"]] = r

    groups = {}
    for r in ordered:
        groups.setdefault(
            (
                r["source"],
                r.get("source_account_id"),
                r.get("conversation_id", r.get("session_id")),
            ),
            [],
        ).append(r)
    # Both ends of the principal conversations, then changes and high-relevance causes.
    for group in sorted(
        groups.values(),
        key=lambda g: max(relevance(r["quote"], terms) for r in g),
        reverse=True,
    )[:3]:
        add(group[0])
        add(group[-1])
    signals = [r for r in reversed(ordered) if STATE_HINT.search(r["quote"])]
    for r in signals[: 6 if query_intent in {"latest", "timeline"} else 4]:
        add(r)
    for r in sorted(ordered, key=lambda r: relevance(r["quote"], terms), reverse=True)[
        :6
    ]:
        add(r)
    remaining = limit - len(selected)
    for i in range(remaining):
        add(ordered[round(i * (len(ordered) - 1) / max(1, remaining - 1))])
    for r in reversed(ordered):
        add(r)
    return sorted(selected.values(), key=lambda r: (r.get("sent_at") or 0, r["id"]))
