"""Bounded provenance/chronology sampling, without story-specific semantic rules."""

import json
from collections import deque


def select_evidence(
    rows, steps, claims, *, limit=20, byte_limit=28000, focus_scope=None, seen_ids=()
):
    if focus_scope is not None:
        return select_blocks(
            rows, steps, claims, focus_scope, seen_ids, limit, byte_limit
        )
    ordered = sorted(rows, key=lambda r: (r.get("sent_at") or 0, r["id"]))
    by_id = {r["id"]: r for r in ordered}
    anchors = []
    for step in steps:
        anchors.extend(step.get("support", []))
        anchors.extend(step.get("targets", []))
    for claim in claims:
        anchors.extend(claim.get("citations", []))
    groups = {}
    for row in ordered:
        key = tuple(
            row.get(k)
            for k in (
                "source",
                "source_account_id",
                "conversation_id",
                "sender_account_id",
                "evidence_type",
            )
        )
        groups.setdefault(key, []).append(row["id"])
    # Keep the seed/each retrieval's earliest and latest available records, then
    # each stable speaker/source's boundary states (including native metadata).
    for ids in [
        [r["id"] for r in ordered if r["id"] in step.get("hits", [])] for step in steps
    ] + list(groups.values()):
        if ids:
            anchors.extend((ids[0], ids[-1]))
    required = list(dict.fromkeys(i for i in anchors if i in by_id))
    # Retain complete recent read blocks before broad historical actor-discovery
    # samples; questions/denials/short answers cannot be reduced to sender boundaries.
    critical = [
        i
        for step in steps
        for i in step.get("support", []) + step.get("targets", [])
        if i in by_id
    ]
    critical += [
        i
        for step in steps
        for i in (step.get("hits", [])[:1] + step.get("hits", [])[-1:])
        if i in by_id
    ]
    critical += [
        i for claim in claims for i in claim.get("citations", []) if i in by_id
    ]
    rooms = {}
    for row in ordered:
        rooms.setdefault(
            (
                row.get("source"),
                row.get("source_account_id"),
                row.get("conversation_id"),
            ),
            [],
        ).append(row["id"])
    critical += [i for ids in rooms.values() for i in (ids[0], ids[-1])]
    priority = (
        critical
        + [
            i
            for step in reversed(steps)
            if step.get("window") != "discovery"
            for i in step.get("hits", [])
            if i in by_id
        ]
        if limit > 20
        else []
    )
    priority.extend(required)
    # Breadth-first temporal sampling across sources keeps intermediate changes
    # represented without guessing cancellation/conflict from a keyword dictionary.
    ranges = deque((ids, 1, len(ids) - 1) for ids in groups.values())
    while ranges:
        ids, a, b = ranges.popleft()
        if a >= b:
            continue
        middle = (a + b) // 2
        priority.append(ids[middle])
        ranges.extend(((ids, a, middle), (ids, middle + 1, b)))
    priority.extend(r["id"] for r in ordered)
    selected = []
    selected_ids = set()
    for identity in dict.fromkeys(priority):
        if len(selected) == limit:
            break
        candidate = selected + [by_id[identity]]
        if len(json.dumps(candidate, ensure_ascii=False).encode("utf-8")) <= byte_limit:
            selected = candidate
            selected_ids.add(identity)
    selected.sort(key=lambda r: (r.get("sent_at") or 0, r["id"]))
    missing = [i for i in required if i not in selected_ids]
    return selected, {
        "strategy": "provenance_anchors_source_boundaries_temporal_sampling",
        "available_count": len(ordered),
        "selected_count": len(selected),
        "omitted_count": len(ordered) - len(selected),
        "required_omitted_count": len(missing),
        "required_omitted_ids": missing[:20],
        "semantic_changes_verified": False,
        "complete_conclusion_allowed": len(selected) == len(ordered),
    }


def select_blocks(rows, steps, claims, scope, seen_ids, limit, byte_limit):
    """Keep episode text blocks and a minimal provenance bridge, not every old speaker."""
    from .recall_evidence_blocks import blocks, conversation

    by_id = {r["id"]: r for r in rows}
    seen = set(seen_ids)
    start, end = scope["sent_from"], scope["sent_to"]
    focus = [r for r in rows if start <= (r.get("sent_at") or -1) <= end]
    mandatory = list(
        dict.fromkeys(i for c in claims for i in c.get("citations", []) if i in by_id)
    )
    if scope.get("identity_evidence"):
        mandatory.extend(
            i
            for step in steps
            if step.get("window") == "identity"
            for i in step.get("record_refs", step.get("hits", []))
            if i in by_id and by_id[i].get("quote", "").strip()
        )
        mandatory = list(dict.fromkeys(mandatory))[:12]
    from .recall_episode_ranking import episode_order

    priority, selected_rooms = episode_order(focus, scope, seen)
    # One actual supporting statement per reached actor/room. Discovery samples
    # remain in the backend; empty undecoded media never consume the text quota.
    bridges = []
    for step in reversed(steps):
        if step.get("tool") == "get_message_context":
            continue
        acquired = [
            by_id[i]
            for i in step.get("record_refs", step.get("hits", []))
            if i in by_id
        ]
        if not any(conversation(r) in selected_rooms for r in acquired):
            continue
        for i in step.get("support", []):
            if i in by_id and by_id[i].get("quote", "").strip() and i not in bridges:
                bridges.append(i)
                break
    # Bridges are mandatory for the selected relationship, with a small explicit cap.
    from .recall_episode_ranking import related_episode_priority

    relation_seats = related_episode_priority(focus, steps, scope, seen, limit)
    context_seats = []
    relation_set = set(relation_seats)
    relation_order = {ident: index for index, ident in enumerate(relation_seats)}
    context_blocks = sorted(
        blocks([by_id[i] for i in relation_seats]),
        key=lambda item: min(relation_order[r["id"]] for r in item[1]),
    )
    for room, part in context_blocks:
        first, last = part[0]["sent_at"] - 120, part[-1]["sent_at"] + 120
        adjacent = sorted(
            [
                r
                for r in focus
                if r["id"] not in relation_set
                and conversation(r) == room
                and first <= r["sent_at"] <= last
                and r.get("retrieval_basis")
                in {"bounded_conversation_context", "reply_reference"}
                and r.get("quote", "").strip()
            ],
            key=lambda r: (r["sent_at"], r["id"]),
        )
        context_seats.extend(r["id"] for r in adjacent[:12])
    context_seats = list(dict.fromkeys(context_seats))
    priority = mandatory + bridges[:4] + relation_seats + context_seats + priority
    if not focus:
        priority.extend(
            r["id"]
            for r in sorted(rows, key=lambda r: (r.get("sent_at") or 0, r["id"]))
            if r.get("quote", "").strip()
        )
    selected = []
    identities = set()
    for identity in dict.fromkeys(priority):
        if len(selected) >= limit:
            break
        value = selected + [by_id[identity]]
        if len(json.dumps(value, ensure_ascii=False).encode()) <= byte_limit:
            selected = value
            identities.add(identity)
    selected.sort(key=lambda r: (r.get("sent_at") or 0, r["id"]))
    required = list(dict.fromkeys(mandatory + bridges[:4]))
    missing = [i for i in required if i not in identities]
    return selected, {
        "strategy": "contiguous_episode_blocks_minimal_provenance",
        "available_count": len(rows),
        "selected_count": len(selected),
        "omitted_count": len(rows) - len(selected),
        "required_omitted_count": len(missing),
        "required_omitted_ids": missing,
        "semantic_changes_verified": False,
        "complete_conclusion_allowed": len(selected) == len(rows),
        "episode_available": len(focus),
        "episode_selected": sum(r["id"] in identities for r in focus),
        "new_episode_selected": sum(
            r["id"] in identities and r["id"] not in seen for r in focus
        ),
        "undecoded_nontext_omitted": sum(
            not r.get("quote", "").strip() and r["id"] not in identities for r in rows
        ),
        "bridge_ids": bridges[:4],
        "new_relation_candidate_count": len(relation_seats),
        "new_relation_selected_count": sum(i in identities for i in relation_seats),
        "related_context_candidate_count": len(context_seats),
        "related_context_selected_count": sum(i in identities for i in context_seats),
    }
