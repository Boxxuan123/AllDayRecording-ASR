"""Natural plans and bounded, source-verified fragment retrieval."""

import asyncio
import time
from .selection import (
    catalogue,
    room_key,
    validate_task,
    fragment_rows,
    fragment_text,
    pack_fragments,
)
from .model import QUERY_BUDGET
from .raw import tokenize


def valid_focus(self, focus, grant, reference):
    if not focus:
        return None
    rows = [self.store.get(k) for k in focus.get("source_keys", [])]
    rows = [
        r
        for r in rows
        if r
        and grant.allows(r)
        and r.sent_at <= reference
        and focus.get("revisions", {}).get(r.key) == r.revision
    ]
    return {
        "source_keys": [r.key for r in rows],
        "anchors": [
            {
                "source_key": r.key,
                "revision": r.revision,
                "conversation_key": room_key(r),
                "speaker": r.speaker,
                "sent_at": r.sent_at,
                "text": r.text[:160],
            }
            for r in sorted(
                rows, key=lambda r: (-len(set(tokenize(r.text))), r.sent_at, r.key)
            )[:8]
        ],
        "conversation_keys": sorted({room_key(r) for r in rows}),
        "message_start": min((r.sent_at for r in rows), default=None),
        "message_end": max((r.sent_at + 1 for r in rows), default=None),
    }


async def retrieve(
    self,
    question,
    reference,
    grant,
    *,
    plan=True,
    planned=None,
    component="hybrid",
    focus=None,
):
    from .core import PLAN_SCHEMA

    started = time.monotonic()
    allowed = self.visible(grant, reference)
    directory = catalogue(allowed)
    rooms = {r["key"] for r in directory}
    scope = QUERY_BUDGET.get()
    # Bounded native previews help the planner resolve an under-specified event.
    # They are deterministic source data, never summaries or evaluator labels.
    previews = []
    import json

    for ep in self.store.build_episodes():
        members = [self.store.get(m["key"]) for m in ep["records"]]
        members = [
            r for r in members if r and grant.allows(r) and r.sent_at <= reference
        ]
        if not members:
            continue
        nonempty = [r for r in members if r.text.strip()]
        informative = sorted(
            nonempty,
            key=lambda r: (-len(set(tokenize(r.text))), -len(r.text), r.sent_at, r.key),
        )[:2]
        sample = list(
            dict.fromkeys(r.key for r in nonempty[:1] + informative + nonempty[-1:])
        )
        item = {
            "conversation_key": room_key(members[0]),
            "start": members[0].sent_at,
            "end": members[-1].sent_at,
            "excerpts": [
                {
                    "source_key": r.key,
                    "revision": r.revision,
                    "speaker": r.speaker,
                    "text": r.text[:100],
                }
                for r in members
                if r.key in sample
            ],
        }
        if len(json.dumps(previews + [item], ensure_ascii=False).encode()) <= 18000:
            previews.append(item)
    if planned is None and plan:
        planned = await asyncio.to_thread(
            self.model.call,
            "plan",
            {
                "question": question,
                "reference_unix": reference,
                "timezone": "Asia/Singapore",
                "native_conversations": directory,
                "bounded_native_previews": previews,
                "confirmed_query_preferences": self.visible_preferences(allowed),
                "focus": self.valid_focus(focus, grant, reference),
            },
            PLAN_SCHEMA,
            "Interpret the original question into up to 4 executable searches. Native previews are partial hints, not a full answer. Use them to form concrete event queries and investigate contradictions or mistaken dates. Select conversation_keys only from the native directory; candidates are not confirmed identities. message_start/end filter speech time; event_start/end describe event time and only rank. For an evening plan, include earlier daytime speech. Explicit dated conversation questions use separate message windows and conversation selectors. For uncertain relative dates, inspect the bounded previous and following days, including daytime discussion; do not assume a remembered event occurred at the requested time. Keep explicit strict message-date filters unchanged. For an event in a group, search mentions in other permitted conversations too. A follow-up inherits supplied focus unless evidence justifies expansion. Each expansion needs a purpose and basis. Names alone do not merge actors. Preferences are query choices, not identity proofs.",
        )
    elif planned is None:
        native_focus = self.valid_focus(focus, grant, reference)
        context_query = question
        if native_focus:
            context_query += (
                "\nPrior source context: "
                + " / ".join(a["text"] for a in native_focus["anchors"])[:350]
            )
        planned = {
            "tasks": [
                {
                    "query": context_query,
                    "object_hint": "",
                    "event_start": None,
                    "event_end": None,
                }
            ],
            "unknown": [],
        }
    tasks = planned.get("tasks", [])
    if not 1 <= len(tasks) <= 4:
        raise ValueError("INVALID_RETRIEVAL_PLAN")
    fragments, traces = {}, []
    focus_bounds = self.valid_focus(focus, grant, reference)
    for task in tasks:
        task = dict(task)
        if focus_bounds and focus_bounds["source_keys"]:
            # Inherit event scope by default. Only an explicitly grounded expansion
            # may leave it; an unbounded older event cannot enter by similarity.
            grounded = any(
                k in task.get("basis", "") for k in focus_bounds["source_keys"]
            )
            if not grounded:
                task["conversation_keys"] = focus_bounds["conversation_keys"]
                task["message_start"] = max(
                    task.get("message_start") or 0, focus_bounds["message_start"] - 3600
                )
                task["message_end"] = min(
                    task.get("message_end") or reference + 1,
                    focus_bounds["message_end"] + 86400,
                    reference + 1,
                )
        validate_task(task, rooms, reference)
        if scope and time.monotonic() >= scope["deadline"]:
            raise TimeoutError("QUERY_WALL_DEADLINE")
        query = task["query"]
        start, end = task.get("event_start"), task.get("event_end")
        window = {"start": start, "end": end} if start is not None else None
        upstream = (
            await self.backend.retrieve(query, reference, window)
            if component == "hybrid"
            else {}
        )

        # Trace contains pre-ranking candidates, not returned evidence. Never mine it.
        def ids(value):
            if isinstance(value, dict):
                for key in ("document_id", "episode_id"):
                    if value.get(key):
                        yield value[key]
                for k, v in value.items():
                    if k != "trace":
                        yield from ids(v)
            elif isinstance(value, list):
                for v in value:
                    yield from ids(v)

        found = []
        for eid in sorted(set(ids(upstream))):
            ep = self.store.episode(eid)
            if ep:
                found.append(ep)
        raw = await self.raw.search(query, grant)
        combined = {e["id"]: e for e in found + raw}
        pieces = fragment_rows(list(combined.values()), self.store, allowed, task)
        trace = {
            "task": task,
            "upstream": upstream,
            "hindsight_documents": [e["id"] for e in found],
            "raw_documents": [e["id"] for e in raw],
            "ranked_fragments": [],
        }
        traces.append(trace)
        if not pieces:
            continue
        began = time.monotonic()
        scores = await self.backend.reranker.predict(
            [(query, fragment_text(f)) for f in pieces]
        )
        self.backend.record("fragment_rerank", began, {"fragments": len(pieces)})
        ordered = sorted(
            zip(pieces, scores, strict=True), key=lambda x: (-float(x[1]), x[0]["id"])
        )
        best = float(ordered[0][1])
        for rank, (f, score) in enumerate(ordered):
            score = float(score)
            kept = rank < 3 and score >= max(0.05, best * 0.15)
            trace["ranked_fragments"].append(
                {
                    "id": f["id"],
                    "score": score,
                    "kept": kept,
                    "keys": [r.key for r in f["rows"]],
                }
            )
            if not kept:
                continue
            item = {
                **f,
                "score": score,
                "purpose": task.get("purpose", "direct"),
                "tasks": [query],
                "hindsight": bool(set(f["episode_ids"]) & {e["id"] for e in found}),
            }
            old = fragments.get(f["id"])
            if not old or old["score"] < score:
                fragments[f["id"]] = item
    ordered = sorted(
        fragments.values(),
        key=lambda f: (f["purpose"] == "identity", -f["score"], f["id"]),
    )
    selected = pack_fragments(ordered)
    return {
        "records": [r for f in selected for r in f["rows"]],
        "plan": planned,
        "traces": traces,
        "component": component,
        "fragments": [
            {k: v for k, v in f.items() if k != "rows"}
            | {"keys": [r.key for r in f["rows"]]}
            for f in selected
        ],
        "candidates": [{k: v for k, v in f.items() if k != "rows"} for f in ordered],
        "elapsed_seconds": time.monotonic() - started,
    }
