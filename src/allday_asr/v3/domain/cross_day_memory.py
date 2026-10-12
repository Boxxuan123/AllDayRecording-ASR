"""Evidence-only discussion index. Never owns people, tasks or voice eligibility."""

from copy import deepcopy
from datetime import date
from allday_asr.v3.domain.ids import stable_ulid

INDEX_ID = stable_ulid("cross-day-memory-v1", "bounded-index")
TOPICS = {
    "招聘与机会": ("招聘", "工作机会", "老师", "婉拒", "简历", "面试"),
    "约定与安排": ("约定", "见面", "明天", "下周"),
    "材料与项目": ("材料", "项目", "报告"),
}


def dates(first, last, timezone):
    if timezone != "Asia/Singapore":
        raise ValueError("memory timezone must be Asia/Singapore")
    a, b = date.fromisoformat(first), date.fromisoformat(last)
    if not 0 <= (b - a).days <= 2:
        raise ValueError("memory update requires one to three complete dates")
    return a, b


def contact_id(platform, source, account):
    return stable_ulid("source-contact", platform, source, account)


def project(scope, discussions, decisions):
    rows = deepcopy(discussions)
    by_id = {r["id"]: r for r in rows}
    links = {d["source"]: d for d in decisions if d["action"] in {"link", "detach"}}
    for r in rows:
        r["thing_id"] = r.get("event_id") or r["id"]
        r["link_basis"] = (
            "existing_event" if r.get("event_id") else "independent_discussion"
        )
        d = links.get(r["id"])
        if d and d["action"] == "detach":
            r["thing_id"], r["link_basis"] = r["id"], "manually_detached"
        r["topics"] = [
            name
            for name, words in TOPICS.items()
            if any(w in " ".join(e["quote"] for e in r["evidence"]) for w in words)
        ]

    # Chains are allowed; cycles are rejected by the command path and fenced here too.
    def target(r, seen):
        d = links.get(r["id"])
        if (
            not d
            or d["action"] != "link"
            or d["target"] not in by_id
            or r["id"] in seen
        ):
            return r["thing_id"]
        destination = by_id[d["target"]]
        detached = links.get(destination["id"], {})
        if detached.get("action") == "detach" and detached["target"] in by_id:
            return by_id[detached["target"]]["thing_id"]
        return target(destination, seen | {r["id"]})

    for r in rows:
        if links.get(r["id"], {}).get("action") == "link":
            r["thing_id"], r["link_basis"] = target(r, set()), "human_confirmed"
    things = []
    for key in dict.fromkeys(r["thing_id"] for r in rows):
        timeline = sorted(
            [r for r in rows if r["thing_id"] == key], key=lambda r: r["occurred_at"]
        )
        valid = [r for r in timeline if r["evidence_status"] == "valid"]
        things.append(
            {
                "id": key,
                "title": timeline[0]["title"],
                "discussion_ids": [r["id"] for r in timeline],
                "last_evidence_at": valid[-1]["occurred_at"] if valid else None,
                "latest_discussion_id": valid[-1]["id"] if valid else None,
                "followup_ids": list(
                    dict.fromkeys(r["event_id"] for r in timeline if r.get("event_id"))
                ),
                "progress": "有依据的记录；尚无后续"
                if len(valid) < 2
                else "请按时间查看各次原文；状态以跟进详情为准",
                "needs_confirmation": any(r.get("pending_change") for r in timeline),
            }
        )
    people = []
    for p in scope["contacts"]:
        interactions = [
            r for r in rows if any(c["id"] == p["id"] for c in r["contacts"])
        ]
        if not interactions:
            continue
        current = next(
            c for r in interactions for c in r["contacts"] if c["id"] == p["id"]
        )
        people.append(
            {
                **current,
                "discussion_ids": [r["id"] for r in interactions],
                "thing_ids": list(dict.fromkeys(r["thing_id"] for r in interactions)),
                "last_evidence_at": max(
                    (
                        r["occurred_at"]
                        for r in interactions
                        if r["evidence_status"] == "valid"
                    ),
                    default=None,
                ),
            }
        )
    # Reply edges have concrete original-record references; semantics alone never creates a merge.
    blocked = {
        tuple(sorted((d["source"], d["target"])))
        for d in decisions
        if d["action"] in {"reject", "detach"}
    }
    # A rejected relationship must not return through another record of the same
    # already established thing. Explicit human linking remains available.
    blocked_groups = {
        tuple(sorted((by_id[a]["thing_id"], by_id[b]["thing_id"])))
        for a, b in blocked if a in by_id and b in by_id
    }
    candidates = []
    for r in rows:
        for other in rows:
            if (
                r["id"] >= other["id"]
                or r["thing_id"] == other["thing_id"]
                or tuple(sorted((r["id"], other["id"]))) in blocked
                or tuple(sorted((r["thing_id"], other["thing_id"]))) in blocked_groups
            ):
                continue
            ids = {e.get("record_id") for e in other["evidence"] if e.get("record_id")}
            refs = {
                e.get("reply_record_id")
                for e in r["evidence"]
                if e.get("reply_record_id")
            }
            reverse = {e.get("record_id") for e in r["evidence"] if e.get("record_id")}
            replies = {
                e.get("reply_record_id")
                for e in other["evidence"]
                if e.get("reply_record_id")
            }
            namespaces = {
                (
                    e.get("dataset"),
                    e.get("platform"),
                    e.get("source_account_id"),
                    e.get("conversation_id"),
                )
                for e in r["evidence"]
            }
            other_namespaces = {
                (
                    e.get("dataset"),
                    e.get("platform"),
                    e.get("source_account_id"),
                    e.get("conversation_id"),
                )
                for e in other["evidence"]
            }
            if (
                (ids & refs or reverse & replies)
                and namespaces & other_namespaces
                and r["evidence_status"] == other["evidence_status"] == "valid"
            ):
                candidates.append(
                    {
                        "source": r["id"],
                        "target": other["id"],
                        "reason": "原消息明确回复另一条讨论中的记录；仍需判断是否同一具体事情",
                    }
                )
    topics = [
        {
            "label": label,
            "discussion_ids": [r["id"] for r in rows if label in r["topics"]],
        }
        for label in TOPICS
    ]
    return {
        "id": INDEX_ID,
        "scope": {k: v for k, v in scope.items() if k != "manifest"},
        "people": people,
        "discussions": rows,
        "things": things,
        "topics": [t for t in topics if t["discussion_ids"]],
        "candidates": candidates,
        "limitations": [
            "仅固定输入清单；资料缺失不表示没有互动。",
            "原文为带日期的陈述或转述，不构成永久人物标签。",
            "话题相同不证明是同一事情；未发现消息不证明事情完成。",
        ],
    }
