"""Read bounded cached originals and existing stable Events, with frozen input IDs."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone as datetime_timezone
from zoneinfo import ZoneInfo
from allday_asr.v3.domain.cross_day_memory import dates, contact_id
from allday_asr.v3.domain.ids import stable_ulid
from allday_asr.v3.domain.memory_content import safe_memory, admission
from allday_asr.v3.domain.chat_projection import semantic_text
from allday_asr.v3.application.insight_projections import _period
from allday_asr.v3.domain.discussion_boundaries import split, role, RULE_VERSION


class CrossDayMemorySources:
    def __init__(self, database, uow_factory, chat_path):
        self.database, self.uow_factory, self.chat_path = (
            database,
            uow_factory,
            chat_path,
        )

    def _read(self, first, last, timezone):
        a, b = dates(first, last, timezone)
        start, _ = _period(a, timezone)
        _, end = _period(b, timezone)
        records, links = [], {}
        available = self.chat_path.exists()
        if available:
            with sqlite3.connect(
                "file:" + self.chat_path.as_posix() + "?mode=ro", uri=True
            ) as c:
                for dataset, body in c.execute(
                    "SELECT dataset,body FROM search_index WHERE sent_at>=? AND sent_at<? ORDER BY sent_at,id LIMIT 5001",
                    (start.timestamp(), end.timestamp()),
                ):
                    records.append({**json.loads(body), "dataset": dataset})
                if len(records) > 5000:
                    raise ValueError(
                        "bounded memory input exceeds 5000 cached records; select fewer dates"
                    )
                links = {
                    (r[0], r[1], r[2]): r[3]
                    for r in c.execute(
                        "SELECT platform,source,account,person_id FROM user_account_links"
                    )
                }
        with self.database.read() as c:
            known = {
                r["person_id"]: r["display_name"]
                for r in c.execute(
                    "SELECT person_id,display_name FROM persons WHERE user_confirmed=1"
                )
            }
        contacts, conversations = {}, {}
        for r in records:
            # Unrelated group traffic cannot become personal interaction from cache alone.
            if (
                r.get("conversation_type") != "c2c"
                or r.get("is_from_self") is True
                or not r.get("sender_account_id")
            ):
                continue
            key = contact_id(
                r["platform"], r["source_account_id"], r["sender_account_id"]
            )
            person = links.get(
                (r["platform"], r["source_account_id"], r["sender_account_id"])
            )
            person = person if person in known else None
            contacts[key] = {
                "id": key,
                "person_id": person,
                "label": known.get(person)
                or r.get("sender_display_name")
                or r.get("conversation_display_name")
                or "独立来源联系人",
                "platform": r["platform"],
                "source_account_id": r["source_account_id"],
                "account_id": r["sender_account_id"],
                "identity_basis": "人工账号关联"
                if person
                else "来源账号独立身份；未关联录音人物",
            }
            conv = (
                r["dataset"],
                r["platform"],
                r["source_account_id"],
                r["conversation_id"],
            )
            conversations.setdefault(conv, set()).add(key)
        events = []
        with self.uow_factory().reading() as uow:
            for n in range((b - a).days + 1):
                day = (a + timedelta(days=n)).isoformat()
                for row in uow.insights.daily_event_states(day, timezone):
                    state = uow.knowledge.get_event(row["event_id"])
                    if (
                        state.status.value != "active"
                        or state.derivation_status != "active"
                    ):
                        continue
                    snapshots = state.payload.get("evidence_snapshots", [])
                    if not snapshots:
                        continue
                    events.append(
                        {
                            "id": state.event_id,
                            "revision": state.revision,
                            "title": state.payload.get("title") or "录音中出现的讨论",
                            "evidence": snapshots,
                        }
                    )
        for event in events:
            for e in event["evidence"]:
                participant = e.get("participant", {})
                person = participant.get("person_id")
                if (
                    participant.get("kind") in {"known", "self"}
                    and participant.get("status") != "unresolved"
                    and person in known
                ):
                    contacts[person] = {
                        "id": person,
                        "person_id": person,
                        "label": known[person],
                        "platform": "recording",
                        "source_account_id": "recording",
                        "account_id": person,
                        "identity_basis": "原转写已确认稳定人物身份",
                    }
        followups = {}
        with self.database.read() as c:
            for row in c.execute(
                "SELECT event_id,payload_json FROM event_current_states WHERE json_extract(payload_json,'$.chat_followup') IS NOT NULL"
            ):
                v = json.loads(row["payload_json"])["chat_followup"]
                refs = v.get("evidence", [])
                for e in refs:
                    followups[(v.get("dataset"), e["record_id"])] = row["event_id"]
        coverage = {
            "chat_cache_available": available,
            "record_counts": {
                (a + timedelta(days=n)).isoformat(): sum(
                    start.timestamp() + n * 86400
                    <= r["sent_at"]
                    < start.timestamp() + (n + 1) * 86400
                    for r in records
                )
                for n in range((b - a).days + 1)
            },
            "recording_events": len(events),
            "missing_sources": (["聊天缓存"] if not available else [])
            + (["已完成录音事件"] if not events else []),
        }
        recording_dates = {
            datetime.fromisoformat(e["start_at"].replace("Z", "+00:00"))
            .astimezone(ZoneInfo(timezone))
            .date()
            .isoformat()
            for event in events
            for e in event["evidence"]
        }
        for n in range((b - a).days + 1):
            day = (a + timedelta(days=n)).isoformat()
            if day not in recording_dates:
                coverage["missing_sources"].append(day + "：已完成录音结构化结果")
        return records, contacts, conversations, events, followups, coverage

    def inventory(self, first, last, timezone):
        records, contacts, conversations, events, followups, coverage = self._read(
            first, last, timezone
        )
        for p in contacts.values():
            matching = [
                r
                for r in records
                if r.get("sender_account_id") == p["account_id"]
                and r["platform"] == p["platform"]
                and r["source_account_id"] == p["source_account_id"]
                and r.get("conversation_type") == "c2c"
            ]
            if p["platform"] == "recording":
                matching = [
                    {
                        "sent_at": datetime.fromisoformat(
                            e["start_at"].replace("Z", "+00:00")
                        ).timestamp()
                    }
                    for event in events
                    for e in event["evidence"]
                    if e.get("participant", {}).get("person_id") == p["id"]
                ]
            p["interaction_count"] = len(matching)
            p["dates"] = sorted(
                {
                    datetime.fromtimestamp(r["sent_at"], ZoneInfo(timezone))
                    .date()
                    .isoformat()
                    for r in matching
                }
            )
        return safe_memory({
            "first": first,
            "last": last,
            "timezone": timezone,
            "contacts": list(contacts.values()),
            "coverage": coverage,
            "model_calls": 0,
        })

    def increment(self, scope):
        return self.freeze(scope["first"], scope["last"], scope["timezone"],
                           [p["id"] for p in scope["contacts"]], prior_scope=scope)

    def freeze(self, first, last, timezone, selected, prior_scope=None):
        records, contacts, conversations, events, followups, coverage = self._read(
            first, last, timezone
        )
        if any(key not in contacts for key in selected):
            raise ValueError(
                "contact has no cached direct interaction in selected dates"
            )
        buckets = {}
        for r in records:
            conv = (r["dataset"],r["platform"],r["source_account_id"],r["conversation_id"])
            keys = conversations.get(conv, set()) & set(selected)
            if len(keys)!=1 or r.get("conversation_type")!="c2c" or not r.get("text"):
                continue
            key=next(iter(keys))
            event=followups.get((r["dataset"],r["record_id"]))
            day=datetime.fromtimestamp(r["sent_at"],ZoneInfo(timezone)).date().isoformat()
            buckets.setdefault((key,day,event),[]).append(r)
        groups=[]
        for (key,day,event),batch in buckets.items():
            for group in split(batch):
                groups.append({**group,"key":key,"day":day,"event_id":event})
        # Do not spend bounded slots on opaque attachments/unsolicited marketing.
        group_count = len(groups)
        excluded_groups=[{"processing_status":"excluded","processing_reason":admission({"event_id":g["event_id"],"evidence":[{"source_type":"chat","identity_self":r.get("is_from_self"),"quote":semantic_text(r["text"])} for r in g["records"]]})[1],"source_refs":[{"dataset":r["dataset"],"record_id":r["record_id"],"revision":r["record_revision"]} for r in g["records"]]} for g in groups if not admission({"event_id":g["event_id"],"evidence":[{"source_type":"chat","identity_self":r.get("is_from_self"),"quote":semantic_text(r["text"])} for r in g["records"]]})[0]]
        groups = [g for g in groups if admission({"event_id": g["event_id"], "evidence": [{"source_type": "chat", "identity_self": r.get("is_from_self"), "quote": semantic_text(r["text"])} for r in g["records"]]})[0]]
        # Fair bounded selection: first discussion per selected contact, then chronological remainder.
        picked = []
        for key in selected:
            g = next((g for g in groups if g["key"] == key), None)
            if g:
                picked.append(g)
        picked.extend(g for g in groups if g not in picked)
        manifest = []
        for g in picked:
            evidence = []
            for r in g["records"]:
                reply = r.get("reply") or {}
                evidence.append(
                    {
                        "source_type": "chat",
                        "dataset": r["dataset"],
                        "record_id": r["record_id"],
                        "revision": r["record_revision"],
                        "quote": semantic_text(r["text"])[:600],
                        "speaker": r.get("sender_display_name") or "来源身份未确认",
                        "occurred_at": datetime.fromtimestamp(
                            r["sent_at"], datetime_timezone.utc
                        ).isoformat().replace("+00:00", "Z"),
                        "learned_at": r.get("ingested_at") or r.get("captured_at"),
                        "assertion": "本人原话（行为需原文证明）"
                        if r.get("is_from_self") is True
                        else "他人陈述／转述，未核实为客观事实",
                        "identity_self": r.get("is_from_self"),
                        "is_from_self": r.get("is_from_self"),
                        "identity_basis": "来源消息明确的本人标志及发送账号" if r.get("is_from_self") is True else contacts[g["key"]]["identity_basis"],
                        "role": role(r),
                        "sender_account_id": r.get("sender_account_id"),
                        "platform": r["platform"],
                        "source_account_id": r["source_account_id"],
                        "conversation_id": r["conversation_id"],
                        "reply_record_id": reply.get("record_id")
                        if isinstance(reply, dict)
                        else None,
                    }
                )
            manifest.append(
                {
                    "id": stable_ulid(
                        "memory-discussion",
                        g["records"][0]["dataset"],
                        g["records"][0]["record_id"],
                    ),
                    "title": g["day"]
                    + " · "
                    + contacts[g["key"]]["label"]
                    + "的独立讨论",
                    "contacts": [contacts[g["key"]]],
                    "event_id": g["event_id"],
                    "occurred_at": evidence[0]["occurred_at"],
                    "evidence": evidence,
                    "evidence_status": "valid",
                    "pending_change": False,
                    "boundary_basis": g["boundary_basis"],
                }
            )
        fresh=manifest
        old = json.loads(json.dumps(prior_scope.get("manifest",[]))) if prior_scope else []
        def refs(row):
            return {(e.get("dataset"),e.get("record_id")) for e in row["evidence"] if e["source_type"]=="chat"}
        # Repair core/context membership without renaming existing discussions or
        # silently merging legacy records. A concrete continuation remains reviewable.
        repaired=[]
        used=set()
        for candidate in fresh:
            incoming=refs(candidate)
            matches=[r for r in old if refs(r)&incoming]
            if matches:
                for n,row in enumerate(matches):
                    selected_e=[e for e in candidate["evidence"] if (e.get("dataset"),e.get("record_id")) in refs(row)]
                    if n==len(matches)-1:
                        selected_e.extend(e for e in candidate["evidence"] if not any((e.get("dataset"),e.get("record_id")) in refs(m) for m in matches))
                    selected_e.extend(e for e in row["evidence"] if row["id"] not in used and (e.get("dataset"),e.get("record_id")) not in set().union(*(refs(c) for c in fresh)) and e not in selected_e)
                    if not selected_e: continue
                    # A split can overlap one legacy discussion twice. Retain its ID
                    # on the first object; the second receives its own source anchor.
                    ident=row["id"] if row["id"] not in used else stable_ulid("memory-discussion",selected_e[0]["dataset"],selected_e[0]["record_id"])
                    used.add(ident)
                    snapshots={(e.get("dataset"),e.get("record_id")):e for e in row["evidence"]}
                    selected_e=[{**snapshots.get((e.get("dataset"),e.get("record_id")),e),"role":e.get("role","core"),"is_from_self":e.get("is_from_self"),"identity_basis":e.get("identity_basis")} for e in selected_e]
                    repaired.append({**candidate,"id":ident,"evidence":selected_e,"occurred_at":selected_e[0]["occurred_at"]})
            else: repaired.append(candidate)
        # Missing/revised originals keep their prior snapshots and invalidation path.
        repaired.extend(row for row in old if row["id"] not in used and not refs(row)&set().union(*(refs(c) for c in fresh)))
        oldids={r["id"] for r in old}
        done=[r for r in repaired if r["id"] in oldids]
        waiting=[r for r in repaired if r["id"] not in oldids]
        prior_pending={r["id"] for r in (prior_scope or {}).get("_pending",[])}
        arrivals=[r for r in waiting if r["id"] not in prior_pending]
        backlog=[r for r in waiting if r["id"] in prior_pending]
        # Alternate newly discovered work and FIFO backlog. Neither can starve.
        schedule=[]
        while arrivals or backlog:
            if arrivals: schedule.append(arrivals.pop(0))
            if backlog: schedule.append(backlog.pop(0))
        advance=prior_scope is None or prior_scope.get("_continue") or prior_scope.get("_input_signature") != [[r["dataset"],r["record_id"],r["record_revision"]] for r in records if any(r in g["records"] for g in groups)]
        chosen=schedule[:7] if advance else []
        manifest=done+chosen
        pending=[{**r,"processing_status":"waiting","processing_reason":"bounded_batch_budget"} for r in schedule[len(chosen):]]
        coverage["incremental"]={"excluded_discussions":group_count-len(groups),"deferred_discussions":len(pending),"processed_discussions":len(manifest),"discussion_limit":7}
        for event in (events[: max(0, 7 - len(chosen))] if prior_scope is None else []):
            ev = []
            for e in event["evidence"][:8]:
                ev.append(
                    {
                        "source_type": "recording",
                        "revision": e["revision"],
                        "quote": semantic_text(e["text"])[:600],
                        "occurred_at": datetime.fromisoformat(
                            e["start_at"].replace("Z", "+00:00")
                        )
                        .astimezone(datetime_timezone.utc)
                        .isoformat().replace("+00:00", "Z"),
                        "learned_at": None,
                        "assertion": "录音中出现的讨论；身份与行为以原文为准",
                        "session_id": e["session_id"],
                        "utterance_id": e["utterance_id"],
                        "start_ms": e["start_ms"],
                        "end_ms": e["end_ms"],
                        "speaker": e.get("participant", {}).get("label")
                        if e.get("participant", {}).get("kind") in {"known", "self"}
                        and e.get("participant", {}).get("status") != "unresolved"
                        else "未知说话人 · 录音中出现的讨论",
                    }
                )
            manifest.append(
                {
                    "id": stable_ulid("memory-recording-discussion", event["id"]),
                    "title": "录音中出现的讨论 · " + event["title"],
                    "contacts": [
                        contacts[k]
                        for k in selected
                        if k
                        in {
                            e.get("participant", {}).get("person_id")
                            for e in event["evidence"]
                            if e.get("participant", {}).get("kind") in {"known", "self"}
                            and e.get("participant", {}).get("status") != "unresolved"
                        }
                        and contacts[k]["platform"] == "recording"
                    ],
                    "event_id": event["id"],
                    "event_revision": event["revision"],
                    "occurred_at": ev[0]["occurred_at"],
                    "evidence": ev,
                    "evidence_status": "valid",
                    "pending_change": False,
                }
            )
        return safe_memory({
            "first": first,
            "last": last,
            "timezone": timezone,
            "contacts": [contacts[k] for k in selected],
            "coverage": coverage,
            "manifest": sorted([{**r,"processing_status":"processed","processing_reason":"admitted_evidence_discussion"} for r in manifest],key=lambda r:(r["occurred_at"],r["id"])),
            "_pending": pending,
            "_excluded": excluded_groups,
            "_boundary_version": RULE_VERSION,
            "_input_signature": [[r["dataset"],r["record_id"],r["record_revision"]] for r in records if any(r in g["records"] for g in groups)],
            "model_calls": 0,
            "object_limit": 10,
        })

    def load(self, scope):
        rows = json.loads(json.dumps(scope["manifest"]))
        # Frozen validation resolves only selected identities and cited originals.
        # It does not re-inventory all chats or structured events on every sync.
        contacts={}
        with self.database.read() as core:
            for p in scope["contacts"]:
                source_label=p["label"]
                person=p.get("person_id") if p["platform"]=="recording" else None
                if p["platform"]!="recording" and self.chat_path.exists():
                    with sqlite3.connect("file:"+self.chat_path.as_posix()+"?mode=ro",uri=True) as chat:
                        link=chat.execute("SELECT person_id FROM user_account_links WHERE platform=? AND source=? AND account=?",(p["platform"],p["source_account_id"],p["account_id"])).fetchone()
                        person=link[0] if link else None
                        original=chat.execute("SELECT body FROM search_index WHERE platform=? AND source=? AND sender=? ORDER BY sent_at DESC LIMIT 1",(p["platform"],p["source_account_id"],p["account_id"])).fetchone()
                        source_label=(json.loads(original[0]).get("sender_display_name") if original else None) or "独立来源联系人"
                        if person == "self":
                            self_row=core.execute("SELECT person_id FROM persons WHERE kind='self' AND user_confirmed=1").fetchone()
                            person=self_row[0] if self_row else None
                found=core.execute("SELECT display_name FROM persons WHERE person_id=? AND user_confirmed=1",(person,)).fetchone() if person else None
                contacts[p["id"]]={**p,"person_id":person if found else None,"label":found[0] if found else source_label,"identity_basis":"原转写已确认稳定人物身份" if p["platform"]=="recording" and found else "人工账号关联" if found else "来源账号独立身份；未关联录音人物"}
        with self.database.read() as core:
            chat = (
                sqlite3.connect(
                    "file:" + self.chat_path.as_posix() + "?mode=ro", uri=True
                )
                if self.chat_path.exists()
                else None
            )
            try:
                for r in rows:
                    for p in r["contacts"]:
                        if p["id"] in contacts:
                            p.update(contacts[p["id"]])
                        else:
                            p["person_id"] = None
                            p["identity_basis"] = "来源缺失；原身份依据已失效"
                    valid = True
                    for e in r["evidence"]:
                        e.setdefault("role", "core")
                        e.setdefault("is_from_self", e.get("identity_self"))
                        if not e.get("identity_basis"): e["identity_basis"] = "原转写身份依据" if e["source_type"]=="recording" else "来源消息发送账号及本人标志"
                        if e["source_type"] == "chat":
                            found = (
                                chat.execute(
                                    "SELECT revision,deleted,body FROM records WHERE dataset=? AND id=?",
                                    (e["dataset"], e["record_id"]),
                                ).fetchone()
                                if chat
                                else None
                            )
                            body = json.loads(found[2]) if found else {}
                            # Project the complete cached message before truncation. Legacy
                            # manifests may contain a truncated XML fragment; revisions fence it.
                            if found and found[0] == e["revision"]:
                                e["quote"] = semantic_text(body.get("text") or "")[:600]
                            e["status"] = (
                                "valid"
                                if found
                                and found[0] == e["revision"]
                                and not found[1]
                                and semantic_text(body.get("text") or "").startswith(e["quote"])
                                and body.get("is_from_self") == e.get("identity_self")
                                and body.get("sender_account_id")
                                == e.get("sender_account_id")
                                else "source_changed_or_missing"
                            )
                        else:
                            found = core.execute(
                                "SELECT revision,status FROM utterances WHERE utterance_id=?",
                                (e["utterance_id"],),
                            ).fetchone()
                            e["status"] = (
                                "valid"
                                if found
                                and found[0] == e["revision"]
                                and found[1] == "active"
                                else "source_changed_or_missing"
                            )
                        if e.get("role","core")=="core": valid = valid and e["status"] == "valid"
                    r["evidence_status"] = (
                        "valid" if valid else "source_changed_or_missing"
                    )
                    if r.get("event_id"):
                        event = core.execute(
                            "SELECT revision,payload_json FROM event_current_states WHERE event_id=?",
                            (r["event_id"],),
                        ).fetchone()
                        conflict = core.execute(
                            "SELECT 1 FROM chat_followup_sources WHERE event_id=? AND conflict_json IS NOT NULL",
                            (r["event_id"],),
                        ).fetchone()
                        r["pending_change"] = bool(conflict) or bool(
                            event
                            and r.get("event_revision")
                            and event[0] != r["event_revision"]
                        )
                        if not event:
                            r["evidence_status"] = "source_changed_or_missing"
                return safe_memory(rows)
            finally:
                if chat:
                    chat.close()
