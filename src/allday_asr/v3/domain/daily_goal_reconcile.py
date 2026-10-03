"""Validate goal-level grouping without trusting invented facts or source IDs."""

import copy
import re
from .daily_events import seconds
from .daily_semantics import CONVERSATION_GAP, semantic_payload


def candidate_description(event, index, *, include_outcome=False):
    payload = semantic_payload(event, "context-only", "UTC")
    rows = event["evidence"]
    activities = [p for p in event["segments"] if p["event_type"] == "activity"]
    anchors = (
        ([activities[0]] + ([activities[-1]] if len(activities) > 1 else []))
        if activities
        else []
    )
    wanted = set(payload["title_evidence_utterance_ids"][:2])
    wanted.update(r["utterance_id"] for r in rows[:3] + rows[-3:])
    wanted.update(uid for p in anchors for uid in p["title_evidence_utterance_ids"][:2])
    outcome = payload["outcome"] if isinstance(payload["outcome"], dict) else None
    if include_outcome and outcome:
        wanted.update(outcome["evidence_utterance_ids"])
    return {
        "index": index,
        **({"explicit_outcome": outcome} if include_outcome else {}),
        # Only open context carries a reusable final-goal key. Micro labels are
        # tentative similarities, never legal final continuation identities.
        ("event_key" if index < 0 else "micro_key"): event["key"],
        "title": event.get("goal_title", payload["title"]),
        "core_topic": event.get("goal", payload["topics"][0]),
        # The goal pass consumes existing facts; full citation lists stay local.
        "claims": [{"text": c["text"]} for c in payload["summary_claims"]],
        "activity_context": [
            {
                "title": p["title"],
                "claims": [c["text"] for c in p["claims"]],
                "evidence_utterance_ids": p["title_evidence_utterance_ids"][:2],
            }
            for p in anchors
        ],
        "importance": payload["importance"],
        "event_type": payload["category"],
        "linked_task_ids": payload["linked_task_ids"],
        "start_at": rows[0]["start_at"],
        "end_at": rows[-1]["end_at"],
        "participant_refs": sorted(
            {
                r["participant"]["key"]
                for r in rows
                if r["participant"]["kind"] != "unknown"
            }
        ),
        "unknown_identity_present": any(
            r["participant"]["kind"] == "unknown" for r in rows
        ),
        "evidence": [
            {
                "utterance_id": r["utterance_id"],
                "revision": r["revision"],
                "text_excerpt": r["text"][:160],
                "timestamp": r["start_at"],
            }
            for r in rows
            if r["utterance_id"] in wanted
        ],
    }


def validate_groups(
    groups, candidates, context, descriptions, *, include_outcome=False
):
    seen, keys, previous_index = set(), set(), -1
    for group in groups:
        indices = group["candidate_indices"]
        if (
            not indices
            or any(type(i) is not int or not 0 <= i < len(candidates) for i in indices)
            or indices != sorted(set(indices))
            or seen.intersection(indices)
            or indices[0] <= previous_index
        ):
            raise ValueError(
                "goal groups must partition ordered candidates exactly once"
            )
        seen.update(indices)
        previous_index = indices[0]
        key = group["event_key"]
        if key in keys or (key not in context and not re.fullmatch(r"new:\d+", key)):
            raise ValueError("goal group key is unknown or duplicated")
        keys.add(key)
        members = [candidates[i] for i in indices]
        allowed = {uid for i in indices for uid in description_refs(descriptions[i])}
        if key in context:
            members.insert(0, context[key])
            allowed.update(
                description_refs(
                    candidate_description(
                        context[key], -1, include_outcome=include_outcome
                    )
                )
            )
        title = group["title"].strip()
        if not title or title.startswith("讨论片段：") or len(title) > 80:
            raise ValueError("goal title is missing or a raw excerpt")
        refs = group["title_evidence_utterance_ids"]
        if not refs or not set(refs) <= allowed:
            raise ValueError("goal title evidence was not supplied for its group")
        importance_refs = group["importance_evidence_utterance_ids"]
        if not importance_refs or not set(importance_refs) <= allowed:
            raise ValueError("goal importance evidence was not supplied for its group")
        rows = sorted(
            (r for m in members for r in m["evidence"]),
            key=lambda r: seconds(r["start_at"]),
        )
        gap = max(
            (
                seconds(b["start_at"]) - seconds(a["end_at"])
                for a, b in zip(rows, rows[1:], strict=False)
            ),
            default=0,
        )
        if gap > CONVERSATION_GAP:
            raise ValueError(
                f"goal group {indices} key {key} bridges a long source gap of {gap:.1f}s; split this group"
            )
        if group["event_type"] == "activity" and not any(
            p["event_type"] == "activity" for m in members for p in m["segments"]
        ):
            raise ValueError("activity grouping lacks an already evidenced activity")
    if seen != set(range(len(candidates))):
        raise ValueError("goal grouping omitted a candidate")


def description_refs(description):
    # Grounded claims explicitly supply source IDs even when the raw excerpt
    # budget omits that row. They remain valid citations of the same candidate.
    return {r["utterance_id"] for r in description["evidence"]} | {
        uid
        for claim in description["claims"]
        for uid in claim.get("evidence_utterance_ids", [])
    }


def join_group(group, candidates, previous, key, provenance):
    members = ([previous] if previous else []) + [
        candidates[i] for i in group["candidate_indices"]
    ]
    rows = {r["utterance_id"]: r for m in members for r in m["evidence"]}
    pieces = [copy.deepcopy(p) for m in members for p in m["segments"]]
    history = {
        p.get("input_sha256", str(p)): p
        for m in members
        for p in m.get("goal_provenance", [])
    }
    merged = {
        "key": key,
        "evidence": sorted(
            rows.values(), key=lambda r: (seconds(r["start_at"]), r["utterance_id"])
        ),
        "segments": pieces,
        "goal": group["goal"],
        "goal_title": group["title"],
        "goal_title_evidence_ids": group["title_evidence_utterance_ids"],
        "goal_reason": group["reason"],
        "goal_importance": group["importance"],
        "goal_importance_reason": group["importance_reason"],
        "goal_importance_evidence_ids": group["importance_evidence_utterance_ids"],
        "goal_provenance": list(history.values()) + [provenance],
    }
    merged["goal_event_type"] = group["event_type"]
    return merged


def constrain_normalized_titles(events, source_goals):
    """Aggregation cannot invent a new action in a title from a metaphor/asides.

    Keep an existing supported semantic title of the dominant meaningful goal.
    Evidence count chooses the naming anchor only, never raises importance.
    """
    for event in events:
        ids = {r["utterance_id"] for r in event["evidence"]}
        members = [
            g for g in source_goals if {r["utterance_id"] for r in g["evidence"]} <= ids
        ]
        if not members:
            raise ValueError("normalized event has no complete source goal")
        meaningful = [g for g in members if g["goal_importance"] != "LOW"] or members
        anchor = max(
            meaningful,
            key=lambda g: (len(g["evidence"]), -seconds(g["evidence"][0]["start_at"])),
        )
        event["normalization_proposed_title"] = event["goal_title"]
        event["goal_title"] = anchor["goal_title"]
        event["goal_title_evidence_ids"] = anchor["goal_title_evidence_ids"]
        event["normalization_title_policy"] = "dominant_supported_goal_title"
        event["title_source_goal_key"] = anchor["key"]
    return events
