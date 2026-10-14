"""Targeted evidence validity and read-only source scope adapters.
Only confirmed remote invalidation writes a derived cache marker, never source facts.
"""

import json
import sqlite3
from functools import wraps
from allday_asr.v3.ports.chat_data import ChatDataError
from allday_asr.v3.domain.source_identity import resolve_chat


def bounded_sql(method):
    @wraps(method)
    def call(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            raise ChatDataError("LOCAL_READ_TIMEOUT", 408) from exc

    return call


class RecallState:
    def dataset(self):
        return (
            self.identities.cache.get("dataset", "")
            if self.identities.chat_path.exists()
            else ""
        )

    def validate_record(self, record):
        self.identities.cache._record(record)

    def evidence(self, record, origin, role="match"):
        from .recall_sources import chat_evidence

        return {
            **chat_evidence(
                record,
                self.identities,
                self.identities.people(),
                self.identities.manual(),
                origin,
                role,
            ),
            "dataset": self.dataset(),
        }

    def context(self, row, scope):
        if scope.get("relationship") == "conversation":
            return []  # Already selected the bilateral conversation; never add outsiders.
        part = self.identities.cache.context(row["record_id"], 2, 4)
        records = [
            r
            for r in part["items"]
            if scope["sent_from"] <= r.get("sent_at", -1) <= scope["sent_to"]
        ]
        return [self.evidence(r, "cache", "context") for r in records]

    def filter_context(self, records, scope):
        records = [
            r
            for r in records
            if scope["sent_from"] <= r.get("sent_at", -1) <= scope["sent_to"]
        ]
        if scope.get("relationship") == "conversation":
            from allday_asr.v3.domain.recall_conversation import participants

            records = participants(
                records, self.identities.people(), self.identities.manual(), scope
            )
        return records

    def conversation_evidence(self, records, scope, existing):
        # Existing selected records can establish the other side when a bounded
        # remote page contains only one speaker. They retain their real IDs.
        local = [
            self.identities.cache.record(r["record_id"])
            for r in existing
            if r["source"] != "recording" and r["origin"] == "cache"
        ]
        merged = {r["record_id"]: r for r in local if r}
        for r in records:
            old = merged.get(r["record_id"])
            if not old or r["record_revision"] >= old["record_revision"]:
                merged[r["record_id"]] = r
        selected = self.filter_context(list(merged.values()), scope)
        remote_ids = {r["record_id"] for r in records}
        return [
            self.evidence(r, "mac", "match")
            for r in selected
            if r["record_id"] in remote_ids
        ]

    def authorized_scopes(self, scope):
        ids = self.identities
        if not ids.chat_path.exists():
            return []
        if not scope["person_id"]:
            return [
                r["scope"]
                for r in ids.cache.scopes()
                if r["enabled"]
                and scope["source"] in {"all", r["scope"].get("platform")}
            ][:3]
        from threading import Event
        import time
        from .recall_conversations import conversation_scopes

        scopes = conversation_scopes(ids, scope, Event(), time.monotonic() + 3)
        if scope.get("relationship") == "conversation":
            return scopes
        # Speaker queries stay inside the same historical stable conversations.
        keys = [
            json.loads(k) for k, v in ids.manual().items() if v == scope["person_id"]
        ]
        return [
            {**s, "sender_account_id": sender}
            for s in scopes
            for platform, source, sender in keys
            if s["platform"] == platform and s["source_account_id"] == source
        ][:3]

    def _markers(self, c, rows):
        exists = c.execute(
            "SELECT 1 FROM sqlite_master WHERE name='recall_source_invalidations'"
        ).fetchone()
        if not exists:
            return {}
        records = [r for r in rows if r["source"] != "recording"]
        if not records:
            return {}
        return {
            r["record_id"]: r["invalid_through"]
            for r in c.execute(
                "SELECT record_id,invalid_through FROM recall_source_invalidations WHERE dataset=? AND record_id IN ("
                + ",".join("?" for _ in records)
                + ")",
                (self.dataset(), *[r["record_id"] for r in records]),
            )
        }

    @bounded_sql
    def changed(self, rows):
        from .recall_sources import CANONICAL

        changed = set()
        recordings = [r for r in rows if r["source"] == "recording"]
        chats = [r for r in rows if r["source"] != "recording"]
        if recordings:
            with self.identities.database.read() as c:
                current = {
                    r[0]: r[1]
                    for r in c.execute(
                        "SELECT u.utterance_id,u.revision FROM utterances u JOIN recording_sessions s ON s.session_id=u.session_id WHERE u.status='active' AND s.tombstoned_at IS NULL AND "
                        + CANONICAL
                        + " AND u.utterance_id IN ("
                        + ",".join("?" for _ in recordings)
                        + ")",
                        [r["utterance_id"] for r in recordings],
                    )
                }
            changed.update(
                r["id"]
                for r in recordings
                if current.get(r["utterance_id"]) != r["revision"]
            )
        if chats and self.identities.chat_path.exists():
            with self.identities.cache.connect() as c:
                markers = self._markers(c, chats)
                current = {
                    r["id"]: r
                    for r in c.execute(
                        "SELECT id,revision,deleted FROM records WHERE dataset=? AND id IN ("
                        + ",".join("?" for _ in chats)
                        + ")",
                        (self.dataset(), *[r["record_id"] for r in chats]),
                    )
                }
            for r in chats:
                item = current.get(r["record_id"])
                if (
                    r.get("dataset", self.dataset()) != self.dataset()
                    or markers.get(r["record_id"], -1) >= r["revision"]
                    or (
                        item is not None
                        and (
                            item["revision"] > r["revision"]
                            or item["deleted"]
                            and item["revision"] >= r["revision"]
                        )
                    )
                    or (item is None and r["origin"] == "cache")
                ):
                    changed.add(r["id"])
        return changed

    def mark_changed(self, row, reason):
        if row["source"] == "recording":
            return
        # Monotonic invalid-through watermark survives restart and old replay. It is
        # query-cache validity metadata, not a mutation of records, identities or memory.
        with self.identities.cache.connect() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS recall_source_invalidations(dataset TEXT NOT NULL,record_id TEXT NOT NULL,invalid_through INTEGER NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(dataset,record_id))"
            )
            c.execute(
                "INSERT INTO recall_source_invalidations VALUES(?,?,?,?) ON CONFLICT(dataset,record_id) DO UPDATE SET invalid_through=max(invalid_through,excluded.invalid_through),reason=excluded.reason",
                (self.dataset(), row["record_id"], row["revision"], reason),
            )

    def identity_changed(self, rows):
        from allday_asr.v3.domain.source_identity import effective_self

        people = self.identities.people()
        manual = self.identities.manual()
        for row in rows:
            if row["source"] == "recording":
                if row.get("person_id") and row.get("speaker") != people.get(
                    row["person_id"], {}
                ).get("display_name"):
                    return True
                continue
            rec = {
                "platform": row["source"],
                "source_account_id": row["source_account_id"],
                "sender_account_id": row.get("sender_account_id"),
                "is_from_self": row.get("source_is_from_self"),
            }
            person, _ = resolve_chat(rec, manual, people)
            if (person or "") != row["person_id"] or effective_self(
                rec, manual, people
            ) != row.get("effective_is_self"):
                return True
        return False
