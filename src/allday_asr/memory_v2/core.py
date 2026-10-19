"""One orchestrator for question answering and range overviews."""

import asyncio
import json
import re
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from dataclasses import asdict
from .contracts import digest
from .raw import RawIndex
from .selection import room_key, wire_record, catalogue, validate_task
from .query import retrieve, valid_focus
from .model import query_scope
from .contracts import strict_schema

PLAN_SCHEMA = strict_schema(
    {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "object_hint": {"type": "string"},
                        "conversation_keys": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 6,
                        },
                        "message_start": {"type": ["integer", "null"]},
                        "message_end": {"type": ["integer", "null"]},
                        "purpose": {
                            "type": "string",
                            "enum": [
                                "direct",
                                "identity",
                                "followup",
                                "date_check",
                                "context",
                            ],
                        },
                        "basis": {"type": "string"},
                        "event_start": {"type": ["integer", "null"]},
                        "event_end": {"type": ["integer", "null"]},
                    },
                },
            },
            "unknown": {"type": "array", "items": {"type": "string"}},
        },
    }
)
ANSWER_SCHEMA = strict_schema(
    {
        "type": "object",
        "properties": {
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "enum": [
                                "statement",
                                "plan",
                                "inference",
                                "completed_report",
                                "unknown",
                            ],
                        },
                        "citations": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "handle": {"type": "string"},
                                    "quote": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
            "unknown": {"type": "array", "items": {"type": "string"}},
        },
    }
)


ANSWER_SCHEMA["properties"]["retrieval_actions"] = PLAN_SCHEMA["properties"]["tasks"]
ANSWER_SCHEMA["required"].append("retrieval_actions")


class MemoryCore:
    def __init__(self, store, backend, model, *, preferences=None):
        self.preferences = preferences or []
        self.store = store
        self.backend = backend
        self.model = model
        self.raw = RawIndex(store, backend)
        self.audit = store.root / "queries"
        self.audit.mkdir(exist_ok=True)

    async def ingest(self, records, *, cancel=None, batch_size=8, max_batches=8):
        if (self.model.root.parent / "CANCEL").exists():
            raise RuntimeError("CANCELLED_BY_CHECKPOINT")
        changed = self.store.update(records)
        episodes, obsolete = self.store.plan()
        for eid in obsolete:
            if cancel and cancel.is_set():
                raise RuntimeError("CANCELLED")
            await self.backend.delete(eid)
            self.store.remove_episode(eid)
        count = 0
        for start in range(0, len(episodes), batch_size):
            if count >= max_batches:
                break
            if cancel and cancel.is_set():
                raise RuntimeError("CANCELLED")
            batch = episodes[start : start + batch_size]
            # Persist readiness only after acknowledged upstream retain.
            await self.backend.ingest(batch)
            for ep in batch:
                self.store.ready(ep)
            count += 1
        await self.raw.sync(self.store.build_episodes())
        pending, _ = self.store.plan()
        return {
            "changed_records": len(changed),
            "retained_episodes": min(len(episodes), count * batch_size),
            "obsolete_deleted": len(obsolete),
            "pending": len(pending),
        }

    retrieve = retrieve
    valid_focus = valid_focus

    def visible(self, grant, reference):
        return [r for r in self.store.records(grant) if r.sent_at <= reference]

    def visible_preferences(self, rows):
        rooms = {room_key(r) for r in rows}
        return [p for p in self.preferences if p.get("conversation_key") in rooms]

    async def query(
        self,
        question,
        reference,
        grant,
        *,
        focus=None,
        deadline=None,
        component="hybrid",
    ):
        with query_scope(deadline=deadline) as state:
            async with asyncio.timeout(
                max(0.001, state["deadline"] - time.monotonic())
            ):
                result = await self.answer(
                    question, reference, grant, focus=focus, component=component
                )
            result["query_budget"] = {
                "generation_calls": state["calls"],
                "limit": state["max_calls"],
            }
            (self.audit / result["query_id"] / "state.json").write_text(
                json.dumps(
                    {
                        "reference": reference,
                        "focus": self.valid_focus(focus, grant, reference),
                        "available_evidence": result["focus"],
                        "unresolved": result["unknown"],
                        "budget": result["query_budget"],
                        "component": component,
                    },
                    ensure_ascii=False,
                ),
                "utf-8",
            )
            return result

    async def answer(
        self,
        question,
        reference,
        grant,
        *,
        retrieved=None,
        focus=None,
        component="hybrid",
        round_number=0,
        seen_actions=None,
    ):
        retrieved = retrieved or await self.retrieve(
            question, reference, grant, focus=focus, component=component
        )
        query_id = uuid.uuid4().hex
        mapping = {}
        wire = []
        for r in retrieved["records"]:
            current = self.store.get(r.key)
            if (
                not current
                or current.fingerprint != r.fingerprint
                or not grant.allows(current)
                or current.sent_at > reference
            ):
                continue
            handle = "E" + str(len(wire) + 1)
            item = wire_record(r, handle)
            if len(json.dumps(wire + [item], ensure_ascii=False).encode()) > 42000:
                continue
            wire.append(item)
            mapping[handle] = asdict(r) | {"key": r.key, "fingerprint": r.fingerprint}
        folder = self.audit / query_id
        folder.mkdir()
        room_for_handle = {
            h: digest([r["platform"], r["source_account_id"], r["conversation_id"]])[
                :16
            ]
            for h, r in mapping.items()
        }
        payload = {
            "question": question,
            "reference_unix": reference,
            "timezone": "Asia/Singapore",
            "records": wire,
            "groups": [
                {
                    "conversation_key": k,
                    "timezone": "Asia/Singapore",
                    "start_local": datetime.fromtimestamp(
                        min(
                            x["sent_at"]
                            for x in wire
                            if room_for_handle[x["handle"]] == k
                        ),
                        ZoneInfo("Asia/Singapore"),
                    ).isoformat(),
                    "end_local": datetime.fromtimestamp(
                        max(
                            x["sent_at"]
                            for x in wire
                            if room_for_handle[x["handle"]] == k
                        ),
                        ZoneInfo("Asia/Singapore"),
                    ).isoformat(),
                    "actors": {
                        digest(r.actor)[:16]: {
                            "speaker": r.speaker,
                            "self": r.is_from_self,
                        }
                        for r in retrieved["records"]
                        if room_key(r) == k
                        and r.key in {m["key"] for m in mapping.values()}
                    },
                    "name": next(
                        r.conversation_name
                        for r in retrieved["records"]
                        if room_key(r) == k
                    ),
                    "platform": next(
                        r.platform for r in retrieved["records"] if room_key(r) == k
                    ),
                    "handles": [
                        x["handle"] for x in wire if room_for_handle[x["handle"]] == k
                    ],
                }
                for k in dict.fromkeys(room_for_handle[x["handle"]] for x in wire)
            ],
            "fragments": [
                {
                    "id": f["id"],
                    "purpose": f.get("purpose"),
                    "start_local": datetime.fromtimestamp(
                        min(
                            mapping[h]["sent_at"]
                            for h in mapping
                            if mapping[h]["key"] in f["keys"]
                        ),
                        ZoneInfo("Asia/Singapore"),
                    ).isoformat(),
                    "end_local": datetime.fromtimestamp(
                        max(
                            mapping[h]["sent_at"]
                            for h in mapping
                            if mapping[h]["key"] in f["keys"]
                        ),
                        ZoneInfo("Asia/Singapore"),
                    ).isoformat(),
                    "handles": [h for h in mapping if mapping[h]["key"] in f["keys"]],
                }
                for f in retrieved.get("fragments", [])
                if any(mapping[h]["key"] in f["keys"] for h in mapping)
            ],
            "confirmed_query_preferences": self.visible_preferences(
                self.visible(grant, reference)
            ),
            "retrieval_plan": retrieved.get("plan", {}),
            "native_directory_candidates_not_identity_proof": catalogue(
                self.visible(grant, reference)
            ),
            "focus": self.valid_focus(focus, grant, reference),
            "may_request_evidence": round_number < 2
            and (
                not hasattr(self.model, "ledger")
                or len(self.model.ledger.export()["attempts"]) + 1
                < self.model.ledger.max_calls
            ),
            "coverage": "bounded partial snapshot; missing messages/media and unreported revisions remain unknown",
        }
        (folder / "input.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), "utf-8"
        )
        (folder / "mapping.json").write_text(
            json.dumps(mapping, ensure_ascii=False, indent=2), "utf-8"
        )
        (folder / "retrieval.json").write_text(
            json.dumps(
                {k: v for k, v in retrieved.items() if k != "records"},
                ensure_ascii=False,
                default=str,
            ),
            "utf-8",
        )
        raw = await asyncio.to_thread(
            self.model.call,
            "answer",
            payload,
            ANSWER_SCHEMA,
            "Answer in Chinese. Each factual claim needs exact short quotes and query-local E handles. Separate quoted facts, plans, and inference: explain conventional idioms and everyday allusions using general language knowledge as a clearly labelled likely inference supported by surrounding speech. Uncertainty about exact items or execution should not suppress the likely linguistic interpretation. Do not invent specific orders, private identity or execution. A name resemblance is only a candidate; a direct address and reply can support contextual attribution. Reference-date arithmetic is supplied system context, not a quotation. Explain date corrections using message times. If a specific unresolved gap can be resolved by bounded retrieval, return retrieval_actions (max 2) with executable conversation keys, message windows, purpose and basis citing an E handle. Otherwise return an empty action list and answer. Only use conversation keys visible in records. Do not repeat searches or infer causal/identity links merely because two valid quotations exist. No evidence of completion means unknown, not not-completed.",
        )
        (folder / "raw-answer.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), "utf-8"
        )
        actions = raw.get("retrieval_actions", [])

        def search_signature(task):
            return digest(
                {
                    k: task.get(k)
                    for k in (
                        "query",
                        "conversation_keys",
                        "message_start",
                        "message_end",
                        "event_start",
                        "event_end",
                    )
                }
            )

        seen_actions = set(seen_actions or []) | {
            search_signature(t) for t in retrieved.get("plan", {}).get("tasks", [])
        }
        fresh, rejected_actions = [], []
        rooms = {room_key(r) for r in self.visible(grant, reference)}
        for task in actions[:2]:
            try:
                validate_task(task, rooms, reference)
                signature = search_signature(task)
                anchors = set(
                    re.findall(r"\bE[1-9][0-9]*\b", task.get("basis", ""))
                ) & set(mapping)
                anchors = [
                    h
                    for h in anchors
                    if (current := self.store.get(mapping[h]["key"]))
                    and current.fingerprint == mapping[h]["fingerprint"]
                    and grant.allows(current)
                ]
                if signature in seen_actions or not anchors:
                    raise ValueError("REPEATED_OR_UNGROUNDED_ACTION")
                seen_actions.add(signature)
                fresh.append(task)
            except (ValueError, TypeError) as exc:
                rejected_actions.append({"task": task, "reason": str(exc)})
        if fresh and payload["may_request_evidence"]:
            extra = await self.retrieve(
                question,
                reference,
                grant,
                planned={"tasks": fresh, "unknown": raw.get("unknown", [])},
                component=component,
            )
            old_keys = {r.key for r in retrieved["records"]}
            if any(r.key not in old_keys for r in extra["records"]):
                merged = {r.key: r for r in retrieved["records"] + extra["records"]}
                extra["records"] = sorted(
                    merged.values(), key=lambda r: (room_key(r), r.sent_at, r.key)
                )
                extra["traces"] = retrieved.get("traces", []) + extra["traces"]
                extra["parent_query_id"] = query_id
                result = await self.answer(
                    question,
                    reference,
                    grant,
                    retrieved=extra,
                    focus=focus,
                    component=component,
                    round_number=round_number + 1,
                    seen_actions=seen_actions,
                )
                (folder / "continued.json").write_text(
                    json.dumps(
                        {"child": result["query_id"], "actions": fresh},
                        ensure_ascii=False,
                    ),
                    "utf-8",
                )
                return result
        claims = []
        rejected = []
        for claim in raw.get("claims", []):
            refs = []
            valid = bool(claim.get("citations"))
            for citation in claim.get("citations", []):
                source = mapping.get(citation.get("handle"))
                quote = citation.get("quote", "")
                current = self.store.get(source["key"]) if source else None
                if (
                    not source
                    or not current
                    or current.fingerprint != source["fingerprint"]
                    or not grant.allows(current)
                    or not quote
                    or len(quote) > 300
                    or quote not in current.text
                ):
                    valid = False
                    break
                start = current.text.index(quote)
                raw_start = current.raw_text.find(quote)
                refs.append(
                    {
                        "source_key": current.key,
                        "record_id": current.record_id,
                        "revision": current.revision,
                        "platform": current.platform,
                        "source_account_id": current.source_account_id,
                        "conversation_id": current.conversation_id,
                        "sender_account_id": current.sender_account_id,
                        "sent_at": current.sent_at,
                        "timezone": current.timezone,
                        "quote": quote,
                        "projection_span": [start, start + len(quote)],
                        "raw_span": [raw_start, raw_start + len(quote)]
                        if raw_start >= 0
                        else [0, len(current.raw_text)],
                        "projection": raw_start < 0,
                        "source_locator": current.source_locator,
                    }
                )
            if valid:
                claims.append({**claim, "citations": refs})
            else:
                rejected.append(claim)
        result = {
            "query_id": query_id,
            "claims": claims,
            "unknown": raw.get("unknown", []),
            "rejected_claims": rejected,
            "input_count": len(wire),
            "audit": str(folder.resolve()),
            "status": "partial",
            "bank": self.backend.bank,
            "input_bytes": len(json.dumps(payload, ensure_ascii=False).encode()),
            "focus": {
                "source_keys": [v["key"] for v in mapping.values()],
                "revisions": {v["key"]: v["revision"] for v in mapping.values()},
            },
            "unresolved_actions": actions,
            "rejected_actions": rejected_actions,
        }
        if rejected:
            result["unknown"].append("部分结论未通过出处或当前修订验证，已单独剔除。")
        (folder / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), "utf-8"
        )
        return result

    async def overview(self, start, end, reference, grant):
        # Range enumeration, same authoritative store, grant, model and citation verifier.
        rows = [r for r in self.store.records(grant) if start <= r.sent_at < end]
        rows.sort(key=lambda r: (room_key(r), r.sent_at, r.key))
        pages = []
        for offset in range(0, len(rows), 100):
            retrieved = {
                "records": rows[offset : offset + 100],
                "plan": {"mode": "range-enumeration"},
                "traces": [],
                "candidates": [],
                "elapsed_seconds": 0,
            }
            pages.append(
                await self.answer(
                    f"回顾消息时段 [{start},{end}) 的交流、事件变化、原因和后续；计划与实施分开。",
                    reference,
                    grant,
                    retrieved=retrieved,
                )
            )
        return {
            "bank": self.backend.bank,
            "pages": pages,
            "source_count": len(rows),
            "writes_generated_memory": False,
            "focus": {
                "source_keys": [r.key for r in rows],
                "revisions": {r.key: r.revision for r in rows},
            },
        }
