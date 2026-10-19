"""One orchestrator for question answering and range overviews."""

import asyncio
import json
import time
import uuid
from collections import Counter
from dataclasses import asdict
from .contracts import digest
from .raw import RawIndex, text
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


class MemoryCore:
    def __init__(self, store, backend, model):
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

    async def retrieve(self, question, reference, grant, *, plan=True):
        started = time.monotonic()
        allowed = self.store.records(grant)
        catalogue = sorted(
            {(r.platform, r.conversation_name, r.speaker) for r in allowed}
        )
        if plan:
            planned = await asyncio.to_thread(
                self.model.call,
                "plan",
                {
                    "question": question,
                    "reference_unix": reference,
                    "timezone": "Asia/Singapore",
                    "available_native_labels": catalogue,
                },
                PLAN_SCHEMA,
                "Interpret the original natural question. Produce at most 4 independent semantic searches, each with its own object and event window. Labels are exploration candidates, not confirmed identity. Do not narrow authorization. Include causes/follow-up/contradiction searches when useful. Query-relative dates use reference_unix. Event time is not message time.",
            )
        else:
            planned = {
                "tasks": [
                    {
                        "query": question,
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
        candidates = {}
        traces = []
        for task in tasks:
            query = task.get("query")
            if not isinstance(query, str) or not 0 < len(query) <= 500:
                raise ValueError("INVALID_SUBQUERY")
            start, end = task.get("event_start"), task.get("event_end")
            if start is not None or end is not None:
                if (
                    not isinstance(start, int)
                    or not isinstance(end, int)
                    or not start < end
                ):
                    raise ValueError("INVALID_EVENT_WINDOW")
            window = {"start": start, "end": end} if start is not None else None
            upstream = await self.backend.retrieve(query, reference, window)
            traces.append({"task": task, "upstream": upstream})

            # Only document IDs from engine output can bridge back into authoritative text.
            def ids(value):
                if isinstance(value, dict):
                    if value.get("document_id"):
                        yield value["document_id"]
                    if value.get("episode_id"):
                        yield value["episode_id"]
                    for v in value.values():
                        yield from ids(v)
                elif isinstance(value, list):
                    for v in value:
                        yield from ids(v)

            found = []
            for eid in set(ids(upstream)):
                ep = self.store.episode(eid)
                if ep and all(
                    grant.allows(self.store.get(r["key"])) for r in ep["records"]
                ):
                    found.append(ep)
            raw = await self.raw.search(query, grant)
            combined = {e["id"]: e for e in found + raw}
            if not combined:
                continue
            eps = list(combined.values())
            rank_started = time.monotonic()
            scores = await self.backend.reranker.predict(
                [(query, text(e)) for e in eps]
            )
            self.backend.record("fusion_rerank", rank_started, {"candidates": len(eps)})
            traces[-1]["hindsight_documents"] = [e["id"] for e in found]
            traces[-1]["raw_documents"] = [e["id"] for e in raw]
            ordered = sorted(
                zip(eps, scores, strict=True), key=lambda v: (-float(v[1]), v[0]["id"])
            )
            rooms = Counter()
            kept = 0
            for ep, score in ordered:
                room = self.store.get(ep["records"][0]["key"]).conversation_id
                if rooms[room] >= 3:
                    continue
                rooms[room] += 1
                kept += 1
                entry = candidates.setdefault(
                    ep["id"],
                    {
                        "episode": ep,
                        "score": float(score),
                        "tasks": [],
                        "hindsight": False,
                    },
                )
                entry["score"] = max(entry["score"], float(score))
                entry["tasks"].append(query)
                entry["hindsight"] |= ep["id"] in {e["id"] for e in found}
                if kept >= 6:
                    break
            if time.monotonic() - started > 150:
                raise TimeoutError("RETRIEVAL_DEADLINE")
        selected = sorted(
            candidates.values(), key=lambda e: (-e["score"], e["episode"]["id"])
        )
        # Round-robin message inclusion prevents one busy group consuming the input budget.
        pool = {}
        lists = [list(e["episode"]["records"]) for e in selected]
        for offset in range(max((len(x) for x in lists), default=0)):
            for members in lists:
                if offset < len(members):
                    m = members[offset]
                    r = self.store.get(m["key"])
                    if r and grant.allows(r):
                        pool[r.key] = r
                    if len(pool) >= 140:
                        break
            if len(pool) >= 140:
                break
        # Adjacent utterances restore questions/negations across processing buckets.
        for seed in list(pool.values()):
            neighbours = [
                r
                for r in allowed
                if (r.platform, r.source_account_id, r.conversation_id)
                == (seed.platform, seed.source_account_id, seed.conversation_id)
                and abs(r.sent_at - seed.sent_at) <= 180
            ]
            for r in sorted(
                neighbours, key=lambda r: (abs(r.sent_at - seed.sent_at), r.key)
            )[:4]:
                if len(pool) < 160:
                    pool[r.key] = r
        return {
            "records": list(pool.values()),
            "plan": planned,
            "traces": traces,
            "candidates": [
                {k: v for k, v in e.items() if k != "episode"}
                | {"episode_id": e["episode"]["id"]}
                for e in selected
            ],
            "elapsed_seconds": time.monotonic() - started,
        }

    async def answer(self, question, reference, grant, *, retrieved=None):
        retrieved = retrieved or await self.retrieve(question, reference, grant)
        query_id = uuid.uuid4().hex
        mapping = {}
        wire = []
        for r in retrieved["records"]:
            current = self.store.get(r.key)
            if (
                not current
                or current.fingerprint != r.fingerprint
                or not grant.allows(current)
            ):
                continue
            handle = "E" + str(len(wire) + 1)
            item = {
                "handle": handle,
                "speaker": r.speaker,
                "actor": digest(r.actor)[:16],
                "platform": r.platform,
                "conversation": r.conversation_name,
                "conversation_key": digest(
                    [r.platform, r.source_account_id, r.conversation_id]
                )[:16],
                "self": r.is_from_self,
                "sent_at": r.sent_at,
                "timezone": r.timezone,
                "text": r.text,
            }
            if len(json.dumps(wire + [item], ensure_ascii=False).encode()) > 42000:
                continue
            wire.append(item)
            mapping[handle] = asdict(r) | {"key": r.key, "fingerprint": r.fingerprint}
        folder = self.audit / query_id
        folder.mkdir()
        payload = {
            "question": question,
            "reference_unix": reference,
            "timezone": "Asia/Singapore",
            "records": wire,
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
            "Answer in Chinese, with separate claims and exact short quotes copied from text. Use query-local E handles only. Explain causes and follow-ups where supported. Correct a mistaken date only with evidence. A chat report is not independent verification of execution. Do not treat a suggested identity relation as confirmed.",
        )
        (folder / "raw-answer.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), "utf-8"
        )
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
        rows.sort(key=lambda r: (r.sent_at, r.key))
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
        }
