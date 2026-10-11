"""Bounded day input assembled from existing analysis and private chat cache."""

import json
import sqlite3
from pathlib import Path
from allday_asr.v3.domain.hashing import canonical_json_sha256
from allday_asr.v3.domain.chat_projection import semantic_value
from allday_asr.v3.domain.ids import stable_ulid
from allday_asr.v3.application.insight_projections import _date, _period


class UnifiedReviewSources:
    def __init__(self, database, uow_factory, chat_path: Path):
        self.database, self.uow_factory, self.chat_path = (
            database,
            uow_factory,
            chat_path,
        )

    def load(self, day, timezone):
        start, end = _period(_date(day), timezone)
        sid = stable_ulid("daily-summary", day, timezone)
        evidence, events, tasks, admitted = {}, [], [], set()
        with self.uow_factory().reading() as uow:
            try:
                previous = uow.insights.daily(sid)
                objective = {
                    k: v
                    for k, v in previous["objective"].items()
                    if k
                    in {
                        "headline",
                        "overview",
                        "key_events",
                        "tasks_created",
                        "tasks_completed",
                        "tasks_pending",
                        "people_interacted",
                        "semantic_status",
                    }
                }
            except KeyError:
                objective = {}
            for row in uow.insights.daily_event_states(day, timezone):
                state = uow.knowledge.get_event(row["event_id"])
                if (
                    state.status.value != "active"
                    or state.derivation_status != "active"
                ):
                    continue
                payload = state.payload
                ids = []
                for e in payload.get("evidence_snapshots", []):
                    # Occurrence time, never a deadline mentioned in the text.
                    if (
                        not start
                        <= __import__("datetime").datetime.fromisoformat(
                            e["start_at"].replace("Z", "+00:00")
                        )
                        < end
                    ):
                        continue
                    key = "recording:" + e["utterance_id"]
                    ids.append(key)
                    evidence[key] = {
                        "evidence_id": key,
                        "source_type": "recording",
                        **e,
                        "is_from_self": (
                            True
                            if e.get("participant", {}).get("kind") == "self"
                            else False
                            if e.get("participant", {}).get("kind") == "known"
                            else None
                        ),
                        "event_ids": [
                            state.event_id,
                            *payload.get("linked_task_ids", []),
                        ],
                    }
                if ids:
                    events.append(
                        {
                            "event_id": state.event_id,
                            "revision": state.revision,
                            "title": payload.get("title"),
                            "summary": payload.get("summary"),
                            "outcome": payload.get("outcome"),
                            "evidence_ids": ids,
                            "linked_task_ids": payload.get("linked_task_ids", []),
                        }
                    )
        with self.database.read() as c:
            # Include cached admitted facts/experiences, not just accepted tasks.
            for row in c.execute(
                "SELECT value_json,decision FROM chat_followup_admissions WHERE decision IN ('task','cared','pending')"
            ):
                value = json.loads(row["value_json"])
                for e in value.get("evidence", []):
                    if (
                        e.get("sent_at") is not None
                        and start.timestamp() <= e["sent_at"] < end.timestamp()
                    ):
                        admitted.add(e["record_id"])
            for row in c.execute(
                "SELECT * FROM event_current_states WHERE json_extract(payload_json,'$.daily_event_version') IS NULL"
            ):
                payload = json.loads(row["payload_json"])
                chat = payload.get("chat_followup", {})
                refs = chat.get("evidence", [])
                relevant = [
                    e
                    for e in refs
                    if e.get("sent_at") is not None
                    and start.timestamp() <= e["sent_at"] < end.timestamp()
                ]
                linked = [
                    e for e in evidence.values() if row["event_id"] in e["event_ids"]
                ]
                if not relevant and not linked:
                    continue
                tasks.append(
                    {
                        "event_id": row["event_id"],
                        "revision": row["revision"],
                        "title": payload.get("title"),
                        "status": row["status"],
                        "category": chat.get("category"),
                        "actor_is_from_self": chat.get("actor_is_from_self"),
                        "historical": chat.get("historical"),
                        "progress_unknown": chat.get("progress_unknown"),
                        "pending_change": bool(
                            c.execute(
                                "SELECT 1 FROM chat_followup_sources WHERE event_id=? AND conflict_json IS NOT NULL",
                                (row["event_id"],),
                            ).fetchone()
                        ),
                        "scheduled_time": payload.get("scheduled_time"),
                        "time": chat.get("time"),
                        "evidence_record_ids": [e["record_id"] for e in relevant],
                    }
                )
                admitted.update(e["record_id"] for e in relevant)
        records = []
        cache_available = self.chat_path.exists()
        if cache_available:
            with sqlite3.connect(
                "file:" + self.chat_path.as_posix() + "?mode=ro", uri=True
            ) as c:
                for dataset, body in c.execute(
                    "SELECT dataset,body FROM search_index WHERE sent_at>=? AND sent_at<? ORDER BY sent_at,id",
                    (start.timestamp(), end.timestamp()),
                ):
                    r = semantic_value(json.loads(body))
                    r["dataset"] = dataset
                    records.append(r)
        # Direct discussions and self speech are evidence of interaction, not proof of a task.
        selected = set(
            i
            for i, r in enumerate(records)
            if r.get("conversation_type") == "c2c"
            or r.get("is_from_self") is True
            or r["record_id"] in admitted
        )
        seed = set(selected)
        for i in seed:
            r = records[i]
            for j in range(max(0, i - 2), min(len(records), i + 3)):
                other = records[j]
                if all(
                    r.get(k) == other.get(k)
                    for k in ("platform", "source_account_id", "conversation_id")
                ):
                    selected.add(j)
        for i in sorted(selected):
            r = records[i]
            key = "chat:" + canonical_json_sha256([r["dataset"], r["record_id"]])[:24]
            event_ids = [
                t["event_id"]
                for t in tasks
                if r["record_id"] in t["evidence_record_ids"]
            ]
            evidence[key] = {
                "evidence_id": key,
                "source_type": "chat",
                "record_id": r["record_id"],
                "revision": r["record_revision"],
                "dataset": r["dataset"],
                "platform": r["platform"],
                "source_account_id": r["source_account_id"],
                "conversation_id": r["conversation_id"],
                "conversation_type": r.get("conversation_type"),
                "conversation_label": r.get("conversation_display_name"),
                "speaker": r.get("sender_display_name") or "身份未确认",
                "is_from_self": r.get("is_from_self"),
                "identity_basis": r.get("self_identity_basis"),
                "sent_at": r["sent_at"],
                "text": r.get("text") or "",
                "context": {
                    k: r.get(k) for k in ("quote", "reply", "forward") if r.get(k)
                },
                "event_ids": event_ids,
                "relevance": "admitted"
                if r["record_id"] in admitted
                else "interaction_context",
            }
        coverage = {
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "chat_cache_available": cache_available,
            "chat_records_in_day": len(records),
            "chat_records_selected": len(selected),
            "chat_records_excluded": len(records) - len(selected),
            "recording_events": len(events),
            "recording_evidence": sum(
                e["source_type"] == "recording" for e in evidence.values()
            ),
            "scope": "仅现有缓存和已完成的结构化结果；不代表全天资料完整。",
            "missing_sources": (["chat"] if not selected else [])
            + (["recording"] if not events else []),
        }
        return {
            "version": "unified-daily-review-v1",
            "date": day,
            "timezone": timezone,
            "coverage": coverage,
            "evidence": list(evidence.values()),
            "recording_events": events,
            "followups": tasks,
            "existing_recording_summary": objective,
        }
