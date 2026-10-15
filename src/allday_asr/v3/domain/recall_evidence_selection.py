"""Bounded provenance/chronology sampling, without story-specific semantic rules."""

import json
from collections import deque


def select_evidence(rows, steps, claims):
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
    priority = list(required)
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
        if len(selected) == 20:
            break
        candidate = selected + [by_id[identity]]
        if len(json.dumps(candidate, ensure_ascii=False).encode("utf-8")) <= 28000:
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
