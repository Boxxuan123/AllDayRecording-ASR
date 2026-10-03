"""Window-independent reconciliation and strict source-index validation."""

import re
from .daily_events import seconds, meaningful_event

SEMANTIC_VERSION = "daily-semantic-v1.2"
MICRO_SECONDS = 300
MICRO_CHARACTERS = 6000
MICRO_ROWS = 160
CONVERSATION_GAP = 300


def micro_batches(rows):
    batches = []
    for row in rows:
        batch = batches[-1] if batches else None
        if (
            not batch
            or len(batch) >= MICRO_ROWS
            or sum(len(r["text"]) for r in batch) + len(row["text"]) > MICRO_CHARACTERS
            or seconds(row["end_at"]) - seconds(batch[0]["start_at"]) > MICRO_SECONDS
        ):
            batches.append([row])
        else:
            batch.append(row)
    return batches


def validate_segments(segments, rows, context_keys):
    offset = 0
    for segment in segments:
        lo, hi = segment["start_index"], segment["end_index"]
        if (
            type(lo) is not int
            or type(hi) is not int
            or lo != offset
            or not lo <= hi < len(rows)
        ):
            raise ValueError(
                "semantic ranges must partition current sources exactly once"
            )
        offset = hi + 1
        key = segment["event_key"]
        if key not in context_keys and not re.fullmatch(r"new:\d+", key):
            raise ValueError("semantic continuation key is not in provided context")
        references = [segment["title_evidence_indices"]] + [
            c["evidence_indices"] for c in segment["claims"]
        ]
        if segment["explicit_outcome"]:
            references.append(segment["explicit_outcome"]["evidence_indices"])
        if any(
            type(i) is not int or not lo <= i <= hi
            for values in references
            for i in values
        ):
            raise ValueError("semantic prose evidence is outside its segment")
        if segment["classification"] != "CORE":
            if any(rows[i].get("task_ids") for i in range(lo, hi + 1)):
                raise ValueError("authoritative task source cannot be classified as incidental/filler")
            continue
        title = segment["title"].strip()
        if not title or len(title) > 80 or title.startswith("讨论片段："):
            raise ValueError("semantic title is missing or a raw excerpt")
        if not segment["claims"] or not segment["title_evidence_indices"]:
            raise ValueError("semantic product text requires evidence")
        lists = [
            segment["title_evidence_indices"],
            *[c["evidence_indices"] for c in segment["claims"]],
        ]
        outcome = segment["explicit_outcome"]
        if outcome:
            lists.append(outcome["evidence_indices"])
        if any(
            not values or any(type(i) is not int or not lo <= i <= hi for i in values)
            for values in lists
        ):
            raise ValueError("semantic prose evidence is outside its segment")
        if outcome:
            text = "".join(rows[i]["text"] for i in outcome["evidence_indices"])
            quote = outcome["quote"]
            if quote not in text or re.search(
                r"[?？]|要不要|是否|建议|如果|要不|可以考虑", quote
            ):
                segment["explicit_outcome"] = None
    if offset != len(rows):
        raise ValueError("semantic output omitted current sources")
    return segments


def reconcile_segments(segments, *, merge_keys=True, preliminary_gate=True):
    # Persistence/hysteresis: a brief LOW detour returning to A is incidental,
    # unless it carries an authoritative task or explicit outcome.
    for i in range(1, len(segments) - 1):
        before, current, after = segments[i - 1 : i + 2]
        evidence = current["evidence"]
        duration = seconds(evidence[-1]["end_at"]) - seconds(evidence[0]["start_at"])
        shared = {r["participant"]["key"] for r in before["evidence"]} & {
            r["participant"]["key"] for r in after["evidence"]
        }
        if (
            preliminary_gate
            and before["key"] == after["key"]
            and current["key"] != before["key"]
            and duration <= 45
            and len(evidence) <= 6
            and current["importance"] == "LOW"
            and shared
            and not current.get("explicit_outcome")
            and not any(r.get("task_ids") for r in evidence)
        ):
            current["classification"] = "INCIDENTAL"
            current["boundary_reason"] = "incidental_return"
    events = []
    for segment in segments:
        if segment["classification"] != "CORE":
            continue
        rows = segment["evidence"]
        if preliminary_gate and not meaningful_event({"topics": set(), "evidence": rows}):
            continue
        previous = (
            next((e for e in reversed(events) if e["key"] == segment["key"]), None)
            if merge_keys
            else None
        )
        gap = (
            seconds(rows[0]["start_at"]) - seconds(previous["evidence"][-1]["end_at"])
            if previous
            else None
        )
        if previous and gap <= CONVERSATION_GAP:
            previous["evidence"].extend(rows)
            previous["segments"].append(segment)
        else:
            events.append(
                {"key": segment["key"], "evidence": list(rows), "segments": [segment]}
            )
    return events


def semantic_payload(event, day, timezone):
    rows, pieces = event["evidence"], event["segments"]
    tasks = sorted({t for r in rows for t in r.get("task_ids", [])})
    rank = {"LOW": 0, "MEDIUM": 5, "HIGH": 10}
    best = max(
        enumerate(pieces),
        key=lambda pair: (
            bool(pair[1]["explicit_outcome"]),
            rank[pair[1]["importance"]],
            pair[0],
        ),
    )[1]
    importance = event.get("goal_importance") or max(
        (p["importance"] for p in pieces), key=rank.get
    )
    if tasks:
        importance = "HIGH"
    claims = [c for p in pieces for c in p["claims"]]
    # Preserve an initial context claim and the most recent result/context claim.
    chosen = [claims[0]] if claims else []
    if claims and claims[-1] != claims[0]:
        chosen.append(claims[-1])
    participants = {r["participant"]["key"]: r["participant"] for r in rows}
    return {
        "daily_event_version": SEMANTIC_VERSION,
        "local_date": day,
        "timezone": timezone,
        "start_at": rows[0]["start_at"],
        "end_at": rows[-1]["end_at"],
        "category": "task_context"
        if tasks
        else event.get("goal_event_type", best["event_type"]),
        "title": event.get("goal_title", best["title"]),
        "title_evidence_utterance_ids": event.get(
            "goal_title_evidence_ids", best["title_evidence_utterance_ids"]
        ),
        "summary": " ".join(c["text"] for c in chosen),
        "summary_claims": chosen,
        "importance": importance,
        "summary_visibility": "major" if importance != "LOW" else "secondary",
        "salience": rank[importance]
        + (100 if tasks else 0)
        + (20 if best["explicit_outcome"] else 0),
        "salience_reasons": (["linked_existing_task"] if tasks else [])
        + (["explicit_source_outcome"] if best["explicit_outcome"] else [])
        + [event.get("goal_importance_reason", best["importance_reason"])],
        "importance_evidence_utterance_ids": event.get(
            "goal_importance_evidence_ids", best["title_evidence_utterance_ids"]
        ),
        "participants": list(participants.values()),
        "linked_task_ids": tasks,
        "outcome": best["explicit_outcome"] or "not_inferred",
        "topics": [best["core_topic"]],
        "evidence_snapshots": rows,
        "semantic_status": "complete",
        "semantic_provenance": best.get("provenance", {}),
        "semantic_reconciliation_provenance": event.get("goal_provenance", []),
        "reconciliation": {
            "boundary": pieces[0]["boundary_reason"],
            "merge_reasons": ["semantic_goal_continuation" for _ in pieces[1:]],
            "rule_version": SEMANTIC_VERSION,
            "semantic_key": event["key"],
            "goal_merge_reason": event.get("goal_reason"),
            "micro_chunks": len(pieces),
            "final_duration_limit": None,
            "title_selection_policy": event.get(
                "normalization_title_policy", "goal_reconciliation"
            ),
            "title_source_goal_key": event.get("title_source_goal_key"),
            "normalization_proposed_title": event.get("normalization_proposed_title"),
            "topic_purity": event.get("topic_purity"),
            "source_roles": event.get("source_roles", []),
            "materialization": event.get("materialization"),
            "information_value": event.get("information_value"),
            "information_value_reason": event.get("information_value_reason"),
            "routine_logistics": event.get("routine_logistics", False),
        },
    }
