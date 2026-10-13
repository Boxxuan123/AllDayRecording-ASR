"""Bounded reads of the existing chat and canonical transcript indices."""

import json
from datetime import datetime
from allday_asr.v3.domain.chat_projection import semantic_text
from allday_asr.v3.domain.source_identity import identity_key, resolve_chat

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
        "sender_account_id": record.get("sender_account_id"),
        "identity_key": identity_key(record) or "",
        "person_id": person or "",
        "speaker": people[person]["display_name"]
        if person
        else record.get("sender_display_name") or "未确认发送者",
        "identity_basis": basis,
        "is_from_self": record.get("is_from_self"),
        "quote": text[:1200],
        "quote_truncated": len(text) > 1200,
        "sent_at": record.get("sent_at"),
        "origin": origin,
        "role": role,
        "assertion": "发言／转述；讨论、计划、决定、执行须分别核对原文",
        "reference_notice": "引用、转发、提及对象不作为当前发言者",
    }


class RecallSources:
    def __init__(self, identities):
        self.identities = identities

    def chat(self, scope, terms, limit, cancel, deadline):
        ids = self.identities
        if not ids.chat_path.exists():
            return [], False
        people, manual = ids.people(), ids.manual()
        clauses = ["dataset=?", "sent_at>=?", "sent_at<=?"]
        params = [ids.cache.get("dataset", ""), scope["sent_from"], scope["sent_to"]]
        if scope["source"] in {"qq", "wechat"}:
            clauses.append("platform=?")
            params.append(scope["source"])
        if terms:
            clauses.append(
                "(" + " OR ".join("instr(coalesce(text,''),?)>0" for _ in terms) + ")"
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
            clauses.append("(" + " OR ".join(choices) + ")")
        import time

        with ids.cache.connect() as c:
            c.execute("PRAGMA query_only=ON")
            c.set_progress_handler(
                lambda: int(cancel.is_set() or time.monotonic() > deadline), 1000
            )
            rows = c.execute(
                "SELECT body FROM search_index WHERE "
                + " AND ".join(clauses)
                + " ORDER BY sent_at DESC,id DESC LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
        return [
            chat_evidence(json.loads(r[0]), ids, people, manual, "cache")
            for r in rows[:limit]
        ], len(rows) > limit

    def recordings(self, scope, terms, limit, cancel, deadline):
        import time

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
            clauses.append("(" + " OR ".join("instr(u.text,?)>0" for _ in terms) + ")")
            params.extend(terms)
        if scope["person_id"]:
            clauses.append(ANNOTATED + "=?")
            params.append(scope["person_id"])
        with self.identities.database.read() as c:
            c.execute("PRAGMA busy_timeout=1000")
            c.set_progress_handler(
                lambda: int(cancel.is_set() or time.monotonic() > deadline), 1000
            )
            rows = c.execute(
                "SELECT u.*, "
                + ANNOTATED
                + " AS confirmed_person FROM utterances u JOIN recording_sessions s ON s.session_id=u.session_id WHERE "
                + " AND ".join(clauses)
                + " ORDER BY u.start_at DESC,u.utterance_id DESC LIMIT ?",
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
