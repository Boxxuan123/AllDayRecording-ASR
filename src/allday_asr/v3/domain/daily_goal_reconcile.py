"""Validate goal-level grouping without trusting invented facts or source IDs."""

import copy
import re
import math
import json
from .daily_events import seconds
from .daily_semantics import CONVERSATION_GAP, semantic_payload


def candidate_description(event, index, *, include_outcome=False, include_scope=False):
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
    if include_scope:
        wanted.update(
            rows[i]["utterance_id"] for i in (len(rows) // 3, 2 * len(rows) // 3)
        )
        high = [p for p in event["segments"] if p["importance"] == "HIGH"]
        wanted.update(
            uid
            for p in high[:1] + high[-1:]
            for uid in p["title_evidence_utterance_ids"][:1]
        )
    outcome = payload["outcome"] if isinstance(payload["outcome"], dict) else None
    if include_outcome and outcome:
        wanted.update(outcome["evidence_utterance_ids"])
    return {
        "index": index,
        "topic_purity": {
            k: v
            for k, v in event.get("topic_purity", {}).items()
            if k
            in (
                "domain",
                "specific_topic",
                "active_goal",
                "entities",
                "decision_context",
            )
        },
        "materialization": event.get("materialization"),
        **({"source_topics": _scope_topics(event)} if include_scope else {}),
        "source_count": len(rows),
        "duration_seconds": seconds(rows[-1]["end_at"]) - seconds(rows[0]["start_at"]),
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


def _scope_topics(event):
    """Keep bounded subtopic scope visible when a goal's first/last claims omit it."""
    pieces = event["segments"]
    chosen = {0, len(pieces) // 2, len(pieces) - 1}
    for i, p in enumerate(pieces):
        if len(chosen) >= 5:
            break
        if p.get("explicit_outcome") or p["importance"] == "HIGH":
            chosen.add(i)
    indices = sorted(chosen)
    result = [
        {
            "title": pieces[i]["title"][:80],
            "core_topic": pieces[i]["core_topic"][:120],
            "claim": " ".join(
                dict.fromkeys(
                    c["text"][:100]
                    for c in [pieces[i]["claims"][0], pieces[i]["claims"][-1]]
                )
            )
            if pieces[i]["claims"]
            else "",
            "start_at": pieces[i]["evidence"][0]["start_at"],
            "end_at": pieces[i]["evidence"][-1]["end_at"],
        }
        for i in indices
    ]
    if len(json.dumps(result, ensure_ascii=False)) > 2000:
        for item in result:
            item["claim"] = ""
            item["core_topic"] = item["core_topic"][:80]
    return result


def validate_groups(
    groups,
    candidates,
    context,
    descriptions,
    *,
    include_outcome=False,
    require_purity=False,
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
        if require_purity or "topic_purity" in group:
            validate_purity(
                group,
                candidates,
                descriptions,
                key in context,
                allowed,
                authoritative_task_ids={
                    t
                    for m in members
                    for r in m["evidence"]
                    for t in r.get("task_ids", [])
                },
            )
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


def validate_purity(
    group, candidates, descriptions, continuing, allowed, *, authoritative_task_ids
):
    """Validate scope and policy, not pretend that source references prove semantics."""
    purity = group.get("topic_purity")
    if not purity:
        raise ValueError("specific topic purity is required")
    for field in (
        "domain",
        "specific_topic",
        "active_goal",
        "continuity_reason",
        "information_value_reason",
    ):
        if type(purity.get(field)) is not str or not purity[field].strip():
            raise ValueError(
                "specific purity scope and reasons must be nonempty strings"
            )
    if purity["continuity_basis"] not in (
        "single_topic",
        "same_concrete_project",
        "same_concrete_activity",
        "same_unresolved_decision",
        "same_task",
        "same_object_and_goal",
        "explicit_return",
    ):
        raise ValueError(
            "weak domain/people/time similarity is not concrete continuity"
        )
    domain = purity["domain"].strip().casefold()
    if any(
        purity[k].strip().casefold() == domain
        for k in ("specific_topic", "active_goal")
    ):
        raise ValueError("a domain label cannot be the specific topic or active goal")
    if (
        type(purity["materialize"]) is not bool
        or type(purity["routine_logistics"]) is not bool
    ):
        raise ValueError("materialization and routine flags must be booleans")
    value = purity["information_value"]
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not 0 <= value <= 100
    ):
        raise ValueError("information value must be a finite score in range")
    if (
        len(group["candidate_indices"]) + int(continuing) > 1
        and purity["continuity_basis"] == "single_topic"
    ):
        raise ValueError(
            "merging requires strong concrete continuity, not a broad domain"
        )
    roles = purity["source_roles"]
    if sorted(r["candidate_index"] for r in roles) != group["candidate_indices"]:
        raise ValueError(
            f"source_roles indices {[r['candidate_index'] for r in roles]} must equal THIS group's candidate_indices {group['candidate_indices']} exactly once. Open context does not get roles or standalone groups."
        )
    retained = set(allowed) - {
        uid
        for role in roles
        if role["role"] not in ("CORE", "SUPPORTING")
        for uid in description_refs(descriptions[role["candidate_index"]])
    }
    if not any(r["role"] == "CORE" for r in roles):
        raise ValueError("a semantic group needs a specific core source")
    for role in roles:
        if role["role"] not in (
            "CORE",
            "SUPPORTING",
            "INCIDENTAL",
            "REJECTED_FOR_EVENT",
        ):
            raise ValueError("unknown evidence role")
        refs = role["evidence_utterance_ids"]
        if not refs or not set(refs) <= description_refs(
            descriptions[role["candidate_index"]]
        ):
            raise ValueError("continuity role evidence must come from its own member")
        member = candidates[role["candidate_index"]]
        if role["role"] not in ("CORE", "SUPPORTING"):
            duration = seconds(member["evidence"][-1]["end_at"]) - seconds(
                member["evidence"][0]["start_at"]
            )
            if duration > 45 or member.get("materialization", {}).get("materialize"):
                raise ValueError(
                    "persistent or already meaningful subtopics need an independent semantic group"
                )
        # A literal routine choice is not necessarily consequential. The gate
        # judges its semantic value; authoritative tasks are never suppressed.
        strong = any(r.get("task_ids") for r in member["evidence"])
        if strong and (
            role["role"] not in ("CORE", "SUPPORTING") or not purity["materialize"]
        ):
            raise ValueError("authoritative task cannot be suppressed as an aside")
    for field in ("title_evidence_utterance_ids", "importance_evidence_utterance_ids"):
        if not set(group[field]) <= retained:
            raise ValueError("event prose must not cite incidental or rejected sources")
    if not set(purity["materialization_evidence_utterance_ids"]) <= retained:
        raise ValueError("materialization evidence must belong to retained sources")
    reasons = (
        "task",
        "explicit_decision",
        "explicit_plan",
        "explicit_result",
        "schedule_change",
        "significant_activity",
        "sustained_meaningful_topic",
        "future_memory_value",
    )
    suppressed = ("low_information_fragment", "incidental", "filler", "duplicate")
    if purity["materialization_reason"] not in reasons + suppressed:
        raise ValueError("unknown materialization reason")
    if purity["materialization_reason"] == "task" and not authoritative_task_ids:
        raise ValueError(
            "task materialization requires an already authoritative task link"
        )
    positive = purity["materialization_reason"] in reasons
    if purity["materialize"] != positive:
        raise ValueError("materialization boolean disagrees with its reason")


def description_refs(description):
    # Grounded claims explicitly supply source IDs even when the raw excerpt
    # budget omits that row. They remain valid citations of the same candidate.
    return {r["utterance_id"] for r in description["evidence"]} | {
        uid
        for claim in description["claims"]
        for uid in claim.get("evidence_utterance_ids", [])
    }


def join_group(group, candidates, previous, key, provenance):
    purity = group.get("topic_purity")
    role_map = (
        {r["candidate_index"]: r for r in purity["source_roles"]} if purity else {}
    )
    retained_indices = [
        i
        for i in group["candidate_indices"]
        if not purity or role_map[i]["role"] in ("CORE", "SUPPORTING")
    ]
    members = ([previous] if previous else []) + [
        candidates[i] for i in retained_indices
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
    if purity:
        merged.update(
            {
                "topic_purity": {
                    k: v
                    for k, v in purity.items()
                    if k
                    in (
                        "domain",
                        "specific_topic",
                        "active_goal",
                        "entities",
                        "decision_context",
                        "continuity_basis",
                        "continuity_reason",
                    )
                },
                "source_roles": (
                    [*previous.get("source_roles", [])] if previous else []
                )
                + [
                    {
                        **role_map[i],
                        "source_utterance_ids": [
                            r["utterance_id"] for r in candidates[i]["evidence"]
                        ],
                        "source_revisions": {
                            r["utterance_id"]: r["revision"]
                            for r in candidates[i]["evidence"]
                        },
                    }
                    for i in group["candidate_indices"]
                ],
                "materialization": {
                    "materialize": purity["materialize"],
                    "reason": purity["materialization_reason"],
                    "evidence_utterance_ids": purity[
                        "materialization_evidence_utterance_ids"
                    ],
                },
                "information_value": purity["information_value"],
                "information_value_reason": purity["information_value_reason"],
                "routine_logistics": purity["routine_logistics"],
            }
        )
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
