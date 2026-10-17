"""Production evidence selection, short references and bounded model input packaging."""

from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo
import json
import re
import time
from allday_asr.v3.domain.recall_evidence_selection import select_evidence
from allday_asr.v3.domain.recall_temporal import nearby_scope
from .recall_citation_handles import CitationHandles
from .recall_investigation_budget import normalize_scope


FIELDS = {
    "id",
    "record_id",
    "revision",
    "dataset",
    "source",
    "source_account_id",
    "conversation_id",
    "conversation_type",
    "conversation_display_name",
    "sender_account_id",
    "speaker",
    "source_is_from_self",
    "effective_is_self",
    "quote",
    "sent_at",
    "role",
    "retrieval_basis",
    "relation_ids",
    "evidence_type",
    "origin",
    "person_id",
    "identity_basis",
}


def payload(tools, round_number, question):
    result = tools.result
    requested = normalize_scope(
        deepcopy(result.get("requested_scope", result["scope"]))
    )
    scope = normalize_scope(deepcopy(result["scope"]))
    hard = normalize_scope(deepcopy(tools.root_scope))
    episode = nearby_scope(requested, result["_request"]) or requested
    focus = {
        **requested,
        "sent_from": episode["sent_from"],
        "sent_to": min(
            tools.now,
            max(
                [requested["sent_to"]]
                + [
                    s["sent_to"]
                    for s in result["investigation_steps"]
                    if s.get("window") == "followup"
                ]
            ),
        ),
    }
    focus.update(
        question=question,
        terms=tools.plan.get("terms", []),
        person_mention=tools.plan.get("person_mention", ""),
    )
    focus["identity_evidence"] = bool(
        tools.plan.get("person_mention")
        and (
            tools.plan.get("issue")
            or re.search(r"称呼|外号|对应.{0,8}账号|查清", question)
        )
    )
    body_question = result["_request"]["question"]
    for term in tools.plan.get("_group_discovery_terms", []):
        body_question = re.sub(re.escape(term), " ", body_question, flags=re.I)
    focus["body_question"] = body_question
    focus["observed_sender_refs"] = [
        ref for discovery in result.get("sender_discovery", [])
        for ref in discovery["matched_record_refs"]
    ]
    seen = result.setdefault("_model_seen_ids", [])
    records, selection = select_evidence(
        [{k: v for k, v in row.items() if k in FIELDS} for row in result["evidence"]],
        result["investigation_steps"],
        result.get("claims", []),
        limit=48,
        byte_limit=32000,
        focus_scope=focus,
        seen_ids=seen,
    )
    tools.flow.selected(records, round_number)
    handles = CitationHandles(result)
    encoded = handles.encode(records)
    handles.validate(encoded, {r["id"]: r for r in result["evidence"]})
    tools.model_handles = handles
    reverse = {full: short for short, full in handles.allowed.items()}

    def short_refs(values):
        return [reverse[v] for v in values if v in reverse]

    objects = []
    # Canonical room handles are derived only from validated native namespaces.
    room_refs = {}
    for row in records:
        key = (row["source"], row.get("source_account_id"), row.get("conversation_id"))
        matches = [
            v
            for v in tools.refs.values()
            if v["type"] == "contact"
            and (
                v.get("platform"),
                v.get("source_account_id"),
                v.get("conversation_id"),
            )
            == key
        ]
        if matches:
            value = min(matches, key=lambda v: bool(v.get("sender_account_id")))
            room_refs[key] = value["ref"]
    room_handles = {}

    reached = {
        ref
        for step in result["investigation_steps"]
        for key in ("conversation_refs", "account_refs")
        for ref in step.get(key, [])
    }
    for value in sorted(
        list(tools.refs.values()),
        key=lambda r: (
            r["ref"] not in room_refs.values(),
            r["type"] != "account",
            r["ref"] not in reached,
        ),
    ):
        if value["type"] == "evidence":
            continue
        item = {
            k: deepcopy(v)
            for k, v in value.items()
            if k not in {"source_account_id", "sender_account_id", "relation_account"}
        }
        if value.get("relation_basis"):
            actor = value.get("relation_account", {})
            observed = [
                r["id"]
                for r in records
                if r.get("source") == actor.get("platform")
                and r.get("source_account_id") == actor.get("source_account_id")
                and r.get("sender_account_id") == actor.get("sender_account_id")
                and r.get("conversation_id") == value.get("conversation_id")
                and r.get("source_is_from_self") is False
            ]
            item["relation_evidence"] = observed[:1] or value.get(
                "relation_evidence", []
            )
        for field in ("citations", "evidence_ids", "relation_evidence"):
            if field in item:
                item[field] = short_refs(item[field])
        if (value.get("relation_basis") and not item.get("relation_evidence")) or (
            value["type"] == "account" and not item.get("citations")
        ):
            continue
        if len(json.dumps(objects + [item], ensure_ascii=False).encode()) > 4000:
            continue
        item["native_ref"] = item["ref"]
        item["ref"] = handles.object(value)
        if value["type"] == "contact":
            key = (
                value.get("platform"),
                value.get("source_account_id"),
                value.get("conversation_id"),
            )
            if room_refs.get(key) == value["ref"]:
                room_handles[key] = item["ref"]
        objects.append(item)
    for row in encoded:
        key = (row["source"], row.get("source_account_id"), row.get("conversation_id"))
        if key in room_handles:
            row["conversation_ref"] = room_handles[key]
    reverse_objects = {
        native: short for short, native in handles.object_allowed.items()
    }
    steps = []
    for step in result["investigation_steps"][-6:]:
        part = {
            k: deepcopy(step[k])
            for k in ("tool", "purpose", "window", "conversation_refs", "account_refs")
            if k in step
        }
        for field in ("conversation_refs", "account_refs"):
            if field in part:
                part[field] = [
                    reverse_objects[r] for r in part[field] if r in reverse_objects
                ]
        part.update(
            record_refs=short_refs(step.get("record_refs", step.get("hits", []))),
            record_count=len(step.get("record_refs", step.get("hits", []))),
            support=short_refs(step.get("support", [])),
        )
        steps.append(part)
    if selection["omitted_count"]:
        tools.note(
            "入模优先连续事件片段及最少来源桥接；部分历史线索或未解码媒体未呈现，不能据此确认完整经过。"
        )
    # Describe only actually selected source episodes. This is evidence metadata,
    # never a conclusion about event date or relevance and never a planned window.
    from allday_asr.v3.domain.recall_evidence_blocks import blocks

    episodes = []
    for _key, group in blocks(records):
        timestamps = [r["sent_at"] for r in group]
        first, last = min(timestamps), max(timestamps)
        refs = short_refs([r["id"] for r in group])
        bridge = list(
            dict.fromkeys(
                e
                for step in result["investigation_steps"]
                if any(
                    r["id"] in step.get("record_refs", step.get("hits", []))
                    for r in group
                )
                for e in short_refs(step.get("support", []))
            )
        )
        zone = ZoneInfo(scope["timezone"])
        episodes.append(
            dict(
                ref="P" + str(len(episodes) + 1),
                record_refs=refs,
                message_first=first,
                message_last=last,
                message_first_local=datetime.fromtimestamp(first, zone).isoformat(),
                message_last_local=datetime.fromtimestamp(last, zone).isoformat(),
                relation_support=bridge[:2],
                temporal_position="before_requested"
                if last < requested["sent_from"]
                else "after_requested"
                if first > requested["sent_to"]
                else "overlaps_requested",
            )
        )
    episodes = episodes[:16]
    seconds = max(0, min(25, int(tools.deadline - time.monotonic())))
    left = tools.remaining()
    final_reason = (
        "model_limit"
        if round_number == 4
        else "wall_clock"
        if left["seconds"] < 30
        else "source_budget"
        if left["source_requests"] <= 6 or left["body_requests"] <= 1
        else ""
    )
    result["metrics"].setdefault("model_inputs", []).append(
        {"round": round_number, **handles.receipt(encoded)}
    )
    seen.extend(i for i in reverse if i not in seen)
    return {
        "query_id": result["query_id"],
        "round": round_number,
        "rounds_remaining": 4 - round_number,
        "final_round": bool(final_reason),
        "finalization_reason": final_reason,
        "budget_remaining": tools.remaining(),
        "question": question,
        "local_now": datetime.fromtimestamp(
            tools.now, ZoneInfo(scope["timezone"])
        ).isoformat(),
        "scope": scope,
        "requested_scope": requested,
        "hard_scope": hard,
        "query_reference": {
            "unix": tools.now,
            "local": datetime.fromtimestamp(
                tools.now, ZoneInfo(scope["timezone"])
            ).isoformat(),
            "timezone": scope["timezone"],
        },
        "requested_event_scope": requested,
        "message_search_windows": result.get("attempted_windows", []),
        "exact_search": bool(tools.plan.get("exact")),
        "terms_role": "literal_filter"
        if tools.plan.get("exact")
        else "semantic_exploration",
        "person_mention": tools.plan.get("person_mention", ""),
        "address_resolution_required": bool(tools.plan.get("issue") and tools.plan.get("person_mention")),
        "time_notice": result.get("time_notice", ""),
        "time_unresolved": tools.plan.get("_parse_error", False),
        "actual_message_dates": sorted(
            {
                datetime.fromtimestamp(r["sent_at"], ZoneInfo(scope["timezone"]))
                .date()
                .isoformat()
                for r in records
            }
        ),
        "records": encoded,
        "episode_candidates": episodes,
        "evidence_selection": selection,
        "objects": objects,
        "steps": steps,
        "known_gaps": list(result["coverage"]),
        "model_budget_seconds": seconds,
        "evidence_complete": False,
        "tool_costs": {
            "search_messages": "one physical request per target, at most two",
            "get_related_conversations": "one per actor",
            "get_message_context": "one per anchor, at most 25 rows",
        },
    }
