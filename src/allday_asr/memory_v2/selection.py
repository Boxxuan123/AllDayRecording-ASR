"""Query-scoped evidence selection; never imports evaluation fixtures."""

from collections import defaultdict
import json
from .contracts import digest


def room_key(row):
    return digest([row.platform, row.source_account_id, row.conversation_id])[:16]


def catalogue(rows):
    rooms = {}
    for r in rows:
        item = rooms.setdefault(
            room_key(r),
            {
                "key": room_key(r),
                "name": r.conversation_name,
                "platform": r.platform,
                "historical_names": [],
                "first_message": r.sent_at,
                "last_message": r.sent_at,
                "actors": {},
            },
        )
        if r.conversation_name not in item["historical_names"]:
            item["historical_names"].append(r.conversation_name)
        item["historical_names"].sort()
        item["first_message"] = min(item["first_message"], r.sent_at)
        item["last_message"] = max(item["last_message"], r.sent_at)
        item["actors"][digest(r.actor)[:16]] = r.speaker
    return sorted(rooms.values(), key=lambda x: x["key"])


def validate_task(task, rooms, reference):
    if not isinstance(task.get("query"), str) or not 0 < len(task["query"]) <= 500:
        raise ValueError("INVALID_SUBQUERY")
    keys = task.get("conversation_keys", [])
    if not isinstance(keys, list) or len(keys) > 6 or set(keys) - set(rooms):
        raise ValueError("UNKNOWN_CONVERSATION_SELECTOR")
    for prefix in ("message", "event"):
        start, end = task.get(prefix + "_start"), task.get(prefix + "_end")
        if (start is None) != (end is None) or (
            start is not None
            and (type(start) is not int or type(end) is not int or start >= end)
        ):
            raise ValueError("INVALID_" + prefix.upper() + "_WINDOW")
    if task.get("purpose", "direct") not in {
        "direct",
        "identity",
        "followup",
        "date_check",
        "context",
    }:
        raise ValueError("INVALID_SEARCH_PURPOSE")
    return task


def accepts(row, task):
    keys = task.get("conversation_keys", [])
    if keys and room_key(row) not in keys:
        return False
    start, end = task.get("message_start"), task.get("message_end")
    return start is None or start <= row.sent_at < end


def fragment_rows(episodes, store, allowed, task):
    """Rebuild continuous turns before ranking, independently of retain buckets."""
    keys = {m["key"] for ep in episodes for m in ep["records"]}
    by_room = defaultdict(list)
    for r in allowed:
        if accepts(r, task):
            by_room[room_key(r)].append(r)
    result = []
    for room, rows in sorted(by_room.items()):
        rows.sort(key=lambda r: (r.sent_at, r.key))
        groups, current = [], []
        for r in rows:
            if current and (
                r.sent_at - current[-1].sent_at > 1200
                or sum(len(x.text) for x in current) + len(r.text) > 4200
            ):
                groups.append(current)
                current = []
            current.append(r)
        if current:
            groups.append(current)
        for group in groups:
            if any(r.key in keys for r in group) and any(r.text.strip() for r in group):
                first, last = rows.index(group[0]), rows.index(group[-1])
                # Bounded adjacent native turns bridge sparse conversations. Keep
                # their actual dates; adjacency is context, never causal proof.
                group = [
                    r
                    for r in rows[max(0, first - 2) : last + 3]
                    if group[0].sent_at - 86400
                    <= r.sent_at
                    <= group[-1].sent_at + 86400
                ]
                result.append(
                    {
                        "id": digest([r.key for r in group]),
                        "room": room,
                        "rows": group,
                        "episode_ids": sorted(
                            {
                                e["id"]
                                for e in episodes
                                if any(
                                    m["key"] in {r.key for r in group}
                                    for m in e["records"]
                                )
                            }
                        ),
                    }
                )
    return result


def fragment_text(fragment):
    return "\n".join(
        f"{r.conversation_name} | {r.speaker}: {r.text or '[missing text/media]'}"
        for r in fragment["rows"]
    )


def pack_fragments(fragments, byte_limit=32000):
    """Allocate whole fragments, then order each conversation chronologically."""
    selected, seen, size = [], set(), 0
    for f in fragments:
        rows = [r for r in f["rows"] if r.key not in seen]
        cost = len(
            json.dumps(
                [wire_record(r, "E000") for r in rows], ensure_ascii=False
            ).encode()
        )
        if not rows or size + cost > byte_limit:
            continue
        size += cost
        seen.update(r.key for r in rows)
        selected.append({**f, "rows": rows})
    selected.sort(
        key=lambda f: (f.get("purpose") == "identity", f["room"], f["rows"][0].sent_at)
    )
    return selected


def wire_record(r, handle):
    # Repeated actor labels and local dates live in the group header. The exact
    # per-message Unix timestamp and namespaced actor remain on every message.
    return {
        "handle": handle,
        "actor": digest(r.actor)[:16],
        "sent_at": r.sent_at,
        "text": r.text or "[missing text/media]",
    }
