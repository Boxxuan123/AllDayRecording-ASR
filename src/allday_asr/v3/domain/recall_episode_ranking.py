"""Soft question relevance and fair contiguous context; density is never a score."""

import re
from collections import defaultdict, deque
from .recall_evidence_blocks import blocks
from .recall_language import STATE_HINT

OPERATORS = re.compile(
    r"昨天晚上|前天晚上|昨晚|昨天|前天|今天|晚上|打算|准备|计划|安排|最后|发生了什么|发生什么|什么|哪里|哪儿|怎么|如何|为什么|是否|最后|已经|了吗|我和|我跟|的|\s+"
)


def features(scope):
    text = " ".join([scope.get("question", ""), *scope.get("terms", [])])
    mention = scope.get("person_mention", "")
    if mention:
        text = text.replace(mention, "")
    text = OPERATORS.sub(" ", text)
    out = set()
    for part in re.findall(r"[A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]+", text.casefold()):
        if re.fullmatch(r"[a-z0-9_-]+", part):
            out.add(part)
        elif len(part) == 1:
            out.add(part)
        else:
            out.update(part[i : i + 2] for i in range(len(part) - 1))
    return out


def text_score(text, terms):
    folded = text.casefold()
    return sum(min(len(t), 8) for t in terms if t in folded)


def episode_order(rows, scope, seen_ids=()):
    terms = features(scope)
    seen = set(seen_ids)
    rooms = defaultdict(list)
    for key, group in blocks(rows):
        text = [r for r in group if r.get("quote", "").strip()]
        if not text:
            continue
        # Duplicate media captions/repeated chatter cannot multiply a fragment's priority.
        unique = text
        windows = (
            [unique]
            if len(unique) <= 8
            else [unique[i : i + 8] for i in range(0, len(unique), 5)]
        )
        for window in windows:
            score = max(text_score(r["quote"], terms) for r in window)
            dialogue = any(
                re.search(r"[?？]|吗[。！!]?$|没[。！!]?$", r["quote"]) for r in window
            )
            change = any(STATE_HINT.search(r["quote"]) for r in window)
            fresh = any(r["id"] not in seen for r in window)
            rooms[key].append(
                (
                    (
                        -score,
                        not fresh,
                        not dialogue,
                        not change,
                        window[0]["sent_at"],
                        window[0]["id"],
                    ),
                    window,
                )
            )
    for key in rooms:
        rooms[key].sort(key=lambda p: p[0])
    queues = {key: deque(parts) for key, parts in rooms.items()}
    keys = sorted(queues, key=lambda key: (queues[key][0][0], repr(key)))
    priority = []
    # One bounded context block per room before another block in a prolific room.
    while queues:
        for key in keys:
            if key not in queues:
                continue
            _, window = queues[key].popleft()
            priority.extend(r["id"] for r in window)
            if not queues[key]:
                del queues[key]
    return list(dict.fromkeys(priority)), set(rooms)
