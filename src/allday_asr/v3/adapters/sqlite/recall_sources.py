"""Bounded reads of the existing chat and canonical transcript indices."""

import json
from allday_asr.v3.domain.recall_investigation_scope import platform_allowed
from allday_asr.v3.domain.recall_read_budget import charge, scan_limit
from datetime import datetime
from .recall_state import RecallState, bounded_sql
from .recall_relations import RecallRelations
from .recall_conversations import conversation_records
from allday_asr.v3.domain.recall_language import relevance
from allday_asr.v3.domain.chat_projection import semantic_text
from allday_asr.v3.domain.source_identity import (
    identity_key,
    resolve_chat,
    effective_self,
)

ANNOTATED = """CASE WHEN json_extract(u.evidence_json,'$.annotation_review.dimensions.person') IS NULL
 AND json_extract(u.evidence_json,'$.person_annotation.source') IN ('human','human_cluster')
 THEN json_extract(u.evidence_json,'$.person_annotation.person_id') END"""
CANONICAL = """u.run_id=(SELECT p.run_id FROM processing_runs p
 LEFT JOIN processing_jobs j ON j.run_id=p.run_id
 WHERE p.session_id=u.session_id AND p.status='succeeded'
 AND coalesce(json_extract(j.request_json,'$.admission_mode'),'production')='production'
 AND EXISTS(SELECT 1 FROM utterances a WHERE a.run_id=p.run_id AND a.status='active')
 ORDER BY p.created_at DESC,p.run_id DESC LIMIT 1)"""


def chat_evidence(record, identities, people, manual, origin, role="match"):
    person, basis = resolve_chat(record, manual, people)
    text = semantic_text(record.get("text") or "")
    return {
        "id": "chat:" + record["record_id"],
        "record_id": record["record_id"],
        "revision": record["record_revision"],
        "source": record["platform"],
        "source_account_id": record["source_account_id"],
        "conversation_id": record["conversation_id"],
        "conversation_type": record.get("conversation_type"),
        "conversation_display_name": record.get("conversation_display_name"),
        "reply": {
            k: v
            for k, v in (record.get("reply") or {}).items()
            if k in {"record_id", "sender_account_id", "text", "resolution"}
        },
        "mentions": [
            m
            for m in (record.get("mentions") or [])[:20]
            if isinstance(m, str) and len(m) <= 200
        ],
        "reply_record_id": (record.get("reply") or {}).get("record_id"),
        "quote_record_id": (
            record["quote"] if isinstance(record.get("quote"), dict) else {}
        ).get("record_id"),
        "sender_account_id": record.get("sender_account_id"),
        "identity_key": identity_key(record) or "",
        "person_id": person or "",
        "speaker": people[person]["display_name"]
        if person
        else "我"
        if effective_self(record, manual, people) is True
        else record.get("sender_display_name") or "来源发送者",
        "identity_basis": basis,
        "source_is_from_self": record.get("is_from_self"),
        "effective_is_self": effective_self(record, manual, people),
        "is_from_self": effective_self(record, manual, people),
        "dataset": getattr(identities, "directory_dataset", "")
        or (
            identities.cache.get("dataset", "") if identities.chat_path.exists() else ""
        ),
        "quote": text[:1200],
        "quote_truncated": len(text) > 1200,
        "sent_at": record.get("sent_at"),
        "origin": origin,
        "role": role,
        "assertion": "发言／转述；讨论、计划、决定、执行须分别核对原文",
        "reference_notice": "引用、转发、提及对象不作为当前发言者",
    }


class RecallSources(RecallState, RecallRelations):
    def __init__(self, identities):
        self.identities = identities

    @bounded_sql
    def chat(self, scope, terms, limit, cancel, deadline):
        if not any(platform_allowed(scope, p) for p in ("qq", "wechat")):
            return [], False
        ids = self.identities
        if not ids.chat_path.exists():
            return [], False
        people, manual = ids.people(), ids.manual()
        if scope.get("relationship") == "conversation":
            records, more = conversation_records(
                ids, scope, terms, limit, cancel, deadline
            )
            evidence = [chat_evidence(r, ids, people, manual, "cache") for r in records]
            invalid = self.changed(evidence)
            return [r for r in evidence if r["id"] not in invalid], more
        clauses = ["dataset=?", "sent_at>=?", "sent_at<=?"]
        params = [ids.cache.get("dataset", ""), scope["sent_from"], scope["sent_to"]]
        allowed = [p for p in ("qq", "wechat") if platform_allowed(scope, p)]
        clauses.append("platform IN (" + ",".join("?" for _ in allowed) + ")")
        params.extend(allowed)
        if scope.get("source_senders"):
            clauses.append(
                "sender IN (" + ",".join("?" for _ in scope["source_senders"]) + ")"
            )
            params.extend(scope["source_senders"])
        if scope.get("source_accounts"):
            choices = []
            for account in scope["source_accounts"]:
                choices.append("(platform=? AND source=?)")
                params.extend((account["platform"], account["source_account_id"]))
            clauses.append("(" + " OR ".join(choices) + ")")
        if scope.get("source_contacts"):
            choices = []
            for contact in scope["source_contacts"]:
                choices.append("(platform=? AND source=? AND conversation=?)")
                params.extend(
                    contact[k]
                    for k in ("platform", "source_account_id", "conversation_id")
                )
            clauses.append("(" + " OR ".join(choices) + ")")
        if scope.get("relationship") == "group":
            clauses.append("json_extract(body,'$.conversation_type')='group'")
        search_terms = terms
        terms = []
        if terms:
            clauses.append(
                "("
                + " AND ".join(
                    "instr(lower(coalesce(text,'')),lower(?))>0" for _ in terms
                )
                + ")"
            )
            params.extend(terms)
        if scope["person_id"]:
            person = scope["person_id"]
            key_sql = "json_array(platform,source,sender)"
            keys = [
                k
                for k, v in manual.items()
                if v == person
                or (v == "self" and people.get(person, {}).get("kind") == "self")
            ]
            choices = []
            if keys:
                choices.append(key_sql + " IN (" + ",".join("?" for _ in keys) + ")")
                params.extend(keys)
            if people.get(person, {}).get("kind") == "self":
                condition = "json_extract(body,'$.is_from_self')=1"
                if manual:
                    condition += (
                        " AND "
                        + key_sql
                        + " NOT IN ("
                        + ",".join("?" for _ in manual)
                        + ")"
                    )
                    params.extend(manual)
                choices.append("(" + condition + ")")
            if not choices:
                return [], False
            clauses.append("(" + " AND ".join(choices) + ")")
        import time

        with ids.cache.connect() as c:
            c.execute("PRAGMA query_only=ON")
            c.create_function(
                "recall_score", 1, lambda value: relevance(value or "", terms)
            )
            c.set_progress_handler(
                lambda: int(cancel.is_set() or time.monotonic() > deadline), 1000
            )
            rows = c.execute(
                "SELECT body FROM search_index WHERE "
                + " AND ".join(clauses)
                + " ORDER BY sent_at DESC,id DESC LIMIT ?",
                (*params, scan_limit()),
            ).fetchall()
        candidates = [json.loads(charge(r[0])) for r in rows]
        candidates = [r for r in candidates if all(t.casefold() in (r.get("text") or "").casefold() for t in search_terms)]
        candidates.sort(key=lambda r:(relevance(r.get("text") or "",search_terms),r.get("sent_at") or 0,r["record_id"]),reverse=True)
        records = candidates[:limit]
        if scope.get("source_contacts") and scope.get("relationship") == "speaker":
            from allday_asr.v3.domain.recall_conversation import source_target

            records = [r for r in records if source_target(r, scope["source_contacts"])]
        evidence = [chat_evidence(r, ids, people, manual, "cache") for r in records]
        invalid = self.changed(evidence)
        return [r for r in evidence if r["id"] not in invalid], len(candidates) > limit or len(rows) == 400

    @bounded_sql
    def recordings(self, scope, terms, limit, cancel, deadline):
        import time

        if not platform_allowed(scope, "recording"):
            return [], False
        if scope.get("relationship") in {"conversation", "group"}:
            # Confirmed voices alone cannot establish user-to-person interaction.
            return [], False
        people = self.identities.people()
        clauses = [
            "u.status='active'",
            "s.tombstoned_at IS NULL",
            CANONICAL,
            "unixepoch(u.start_at)>=?",
            "unixepoch(u.start_at)<=?",
        ]
        params = [scope["sent_from"], scope["sent_to"]]
        if terms:
            clauses.append(
                "("
                + " AND ".join("instr(lower(u.text),lower(?))>0" for _ in terms)
                + ")"
            )
            params.extend(terms)
        if scope["person_id"]:
            clauses.append(ANNOTATED + "=?")
            params.append(scope["person_id"])
        with self.identities.database.read() as c:
            c.execute("PRAGMA busy_timeout=1000")
            c.create_function(
                "recall_score", 1, lambda value: relevance(value or "", terms)
            )
            c.set_progress_handler(
                lambda: int(cancel.is_set() or time.monotonic() > deadline), 1000
            )
            rows = c.execute(
                "SELECT u.*, "
                + ANNOTATED
                + " AS confirmed_person FROM utterances u JOIN recording_sessions s ON s.session_id=u.session_id WHERE "
                + " AND ".join(clauses)
                + " ORDER BY recall_score(u.text) DESC,u.start_at DESC,u.utterance_id DESC LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
        result = []
        for r in rows[:limit]:
            person = r["confirmed_person"] if r["confirmed_person"] in people else None
            text = semantic_text(r["text"])
            result.append(
                {
                    "id": "recording:" + r["utterance_id"],
                    "utterance_id": r["utterance_id"],
                    "session_id": r["session_id"],
                    "revision": r["revision"],
                    "source": "recording",
                    "identity_key": "recording:"
                    + r["session_id"]
                    + ":"
                    + (r["speaker_track_id"] or r["utterance_id"]),
                    "original_speaker_track_id": r["original_speaker_track_id"],
                    "speaker_track_id": r["speaker_track_id"],
                    "person_id": person or "",
                    "source_is_from_self": None,
                    "effective_is_self": people[person]["kind"] == "self"
                    if person
                    else None,
                    "is_from_self": people[person]["kind"] == "self"
                    if person
                    else None,
                    "speaker": people[person]["display_name"]
                    if person
                    else "未知说话人",
                    "identity_basis": "原转写人工确认"
                    if person
                    else "未确认，不从聊天推断",
                    "quote": text[:1200],
                    "quote_truncated": len(text) > 1200,
                    "sent_at": int(
                        datetime.fromisoformat(
                            r["start_at"].replace("Z", "+00:00")
                        ).timestamp()
                    ),
                    "start_at": r["start_at"],
                    "end_at": r["end_at"],
                    "start_ms": r["start_ms"],
                    "end_ms": r["end_ms"],
                    "origin": "transcript",
                    "role": "match",
                    "assertion": "录音中出现的陈述；听到不等于本人执行",
                }
            )
        return result, len(rows) > limit
