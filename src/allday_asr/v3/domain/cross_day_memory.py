"""Evidence-only discussion index. Never owns people, tasks or voice eligibility."""

from copy import deepcopy
from datetime import date, datetime
from allday_asr.v3.domain.ids import stable_ulid

INDEX_ID = stable_ulid("cross-day-memory-v1", "bounded-index")
from .memory_content import safe_memory, admission, content, topics as discussion_topics, latest_evidence, continuation_reason


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
    all_rows = safe_memory(deepcopy(discussions))
    protected = {d[k] for d in decisions if d["action"] in {"link", "detach"} for k in ("source", "target")}
    excluded = []
    rows = []
    for r in all_rows:
        admitted, reason = admission(r)
        if not admitted and r["id"] not in protected:
            excluded.append({"id": r["id"], "reason": reason, "source_refs": [{k: e[k] for k in ("dataset", "record_id", "revision", "source_type") if k in e} for e in r["evidence"]]})
            continue
        core={**r,"evidence":[e for e in r["evidence"] if e.get("role","core")=="core"]}
        r["title"], r["summary"] = content(core)
        rows.append(r)
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
        r["topics"] = discussion_topics(r)

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
        valid = sorted([r for r in timeline if r["evidence_status"] == "valid"], key=lambda r: datetime.fromisoformat(latest_evidence(r).replace("Z", "+00:00")))
        things.append(
            {
                "id": key,
                "title": timeline[0]["title"],
                "discussion_ids": [r["id"] for r in timeline],
                "last_evidence_at": max((latest_evidence(r) for r in valid), key=lambda v: datetime.fromisoformat(v.replace("Z", "+00:00")), default=None),
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
                        latest_evidence(r)
                        for r in interactions
                        if r["evidence_status"] == "valid"
                    ),
                    key=lambda v: datetime.fromisoformat(v.replace("Z", "+00:00")),
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
            continuation = continuation_reason(r, other)
            if (
                (ids & refs or reverse & replies or continuation)
                and namespaces & other_namespaces
                and r["evidence_status"] == other["evidence_status"] == "valid"
            ):
                candidates.append(
                    {
                        "source": r["id"],
                        "target": other["id"],
                        "reason": continuation or "原消息明确回复另一条讨论中的记录；仍需判断是否同一具体事情",
                    }
                )
    topics = [
        {
            "label": label,
            "discussion_ids": [r["id"] for r in rows if label in r["topics"]],
        }
        for label in list(dict.fromkeys(t for r in rows for t in r["topics"]))[:3]
    ]
    return {
        "id": INDEX_ID,
        "scope": {k: v for k, v in scope.items() if k != "manifest" and not k.startswith("_")},
        "people": people,
        "discussions": rows,
        "things": things,
        "topics": [t for t in topics if t["discussion_ids"]],
        "candidates": candidates,
        "excluded": excluded,
        "limitations": [
            "仅固定输入清单；资料缺失不表示没有互动。",
            "原文为带日期的陈述或转述，不构成永久人物标签。",
            "话题相同不证明是同一事情；未发现消息不证明事情完成。",
        ],
    }
