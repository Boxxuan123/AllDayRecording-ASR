"""Additive identity observations and durable fixed-ID refresh receipts in the existing cache."""

import json
import time
from allday_asr.v3.domain.chat_data import packed
from allday_asr.v3.domain.chat_identity import (
    checked_identity,
    frozen_record,
    merge_live_identity,
)
from allday_asr.v3.ports.chat_data import ChatDataError

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_identity_observations(
 dataset TEXT,id TEXT,revision INTEGER,platform TEXT,source TEXT,account TEXT,
 is_self INTEGER,basis TEXT,observed_at REAL,PRIMARY KEY(dataset,id));
CREATE INDEX IF NOT EXISTS identity_sender ON chat_identity_observations(dataset,platform,source,account,is_self);
CREATE TABLE IF NOT EXISTS chat_identity_manual_audit(
 id INTEGER PRIMARY KEY,platform TEXT,source TEXT,account TEXT,previous_person TEXT,person_id TEXT,observed_at REAL);
CREATE TABLE IF NOT EXISTS chat_identity_refresh_jobs(id TEXT PRIMARY KEY,dataset TEXT,value TEXT);
CREATE TABLE IF NOT EXISTS chat_identity_refresh_items(
 job TEXT,id TEXT,revision INTEGER,state TEXT,attempts INTEGER DEFAULT 0,error TEXT,PRIMARY KEY(job,id));
"""


class ChatIdentityCache:
    @staticmethod
    def observe_identity(db, dataset, record):
        if "is_from_self" not in record:
            return
        v = checked_identity(record)
        db.execute(
            "INSERT INTO chat_identity_observations VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(dataset,id) DO UPDATE SET revision=excluded.revision,platform=excluded.platform,source=excluded.source,account=excluded.account,is_self=excluded.is_self,basis=excluded.basis,observed_at=excluded.observed_at",
            (
                dataset,
                record["record_id"],
                record["record_revision"],
                record["platform"],
                record["source_account_id"],
                record.get("sender_account_id"),
                v["is_from_self"],
                v["self_identity_basis"],
                time.time(),
            ),
        )

    def identity_refresh_status(self):
        with self.connect() as db:
            row = db.execute(
                "SELECT id,value FROM chat_identity_refresh_jobs ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        value = json.loads(row["value"])
        with self.connect() as db:
            counts = {
                x[0]: x[1]
                for x in db.execute(
                    "SELECT state,COUNT(*) FROM chat_identity_refresh_items WHERE job=? GROUP BY state",
                    (row["id"],),
                )
            }
        return {
            "id": row["id"],
            **value,
            "success": counts.get("done", 0),
            "failed": counts.get("failed", 0),
            "remaining": counts.get("pending", 0),
        }

    def identity_links(self):
        dataset = self.get("dataset")
        with self.connect() as db:
            manual = {
                packed([r["platform"], r["source"], r["account"]]): r["person_id"]
                for r in db.execute("SELECT * FROM user_account_links")
            }
            observed = db.execute(
                "SELECT o.platform,o.source,o.account,sum(o.is_self=1) AS positive,sum(o.is_self=0) AS negative FROM chat_identity_observations o JOIN records r ON r.dataset=o.dataset AND r.id=o.id WHERE o.dataset=? AND r.deleted=0 AND o.account IS NOT NULL AND o.account!='' GROUP BY o.platform,o.source,o.account",
                (dataset,),
            ).fetchall()
            result = dict(manual)
            conflicts = []
            automatic = []
            for row in observed:
                key = packed([row["platform"], row["source"], row["account"]])
                if row["positive"]:
                    proof = [
                        dict(x)
                        for x in db.execute(
                            "SELECT id,revision,basis,observed_at FROM chat_identity_observations WHERE dataset=? AND platform=? AND source=? AND account=? AND is_self=1",
                            (dataset, row["platform"], row["source"], row["account"]),
                        )
                    ]
                    v = {
                        "person_id": "self",
                        "origin": "mac_true",
                        "evidence_count": row["positive"],
                        "evidence": proof,
                    }
                    automatic.append({"key": key, **v})
                    if key not in manual and not row["negative"]:
                        result[key] = v
                if (
                    row["positive"]
                    and (row["negative"] or (key in manual and manual[key] != "self"))
                ) or (row["negative"] and manual.get(key) == "self"):
                    conflicts.append(
                        {
                            "key": key,
                            "reason": "manual_or_message_identity_conflict",
                            "positive": row["positive"],
                            "negative": row["negative"],
                            "manual_person": manual.get(key),
                        }
                    )
            return {
                "links": result,
                "automatic": automatic,
                "manual": manual,
                "conflicts": conflicts,
            }

    def save_manual_identity(self, platform, source, account, person):
        with self.connect() as db:
            old = db.execute(
                "SELECT person_id FROM user_account_links WHERE platform=? AND source=? AND account=?",
                (platform, source, account),
            ).fetchone()
            db.execute(
                "INSERT INTO chat_identity_manual_audit(platform,source,account,previous_person,person_id,observed_at) VALUES(?,?,?,?,?,?)",
                (
                    platform,
                    source,
                    account,
                    old[0] if old else None,
                    person,
                    time.time(),
                ),
            )
            db.execute(
                "INSERT INTO user_account_links VALUES(?,?,?,?) ON CONFLICT(platform,source,account) DO UPDATE SET person_id=excluded.person_id",
                (platform, source, account, person),
            )

    def update_identity(self, dataset, incoming):
        self._record(incoming)
        v = checked_identity(incoming)
        if "is_from_self" not in incoming or "self_identity_basis" not in incoming:
            raise ChatDataError("SELF_IDENTITY_MISSING", 502)
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM records WHERE dataset=? AND id=?",
                (dataset, incoming["record_id"]),
            ).fetchone()
            if not row or row["deleted"]:
                raise ChatDataError("LOCAL_RECORD_UNAVAILABLE", 409)
            old = json.loads(row["body"])
            if row["revision"] != incoming["record_revision"] or frozen_record(
                old
            ) != frozen_record(incoming):
                raise ChatDataError("IMMUTABLE_REVISION_CHANGED", 409)
            body = packed(merge_live_identity(old, incoming))
            db.execute(
                "UPDATE records SET body=? WHERE dataset=? AND id=?",
                (body, dataset, incoming["record_id"]),
            )
            db.execute(
                "UPDATE search_index SET body=? WHERE dataset=? AND id=? AND revision=?",
                (body, dataset, incoming["record_id"], incoming["record_revision"]),
            )
            self.observe_identity(db, dataset, {**old, **v})
            self.refresh_identity_projections(db, dataset, incoming, v)
            return (
                old.get("is_from_self") != v["is_from_self"]
                or old.get("self_identity_basis") != v["self_identity_basis"]
                or "is_from_self" not in old
            )

    def refresh_identity_projections(self, db, dataset, incoming, v):
        # Context/answer records keep their original prose/quotes but get fresh live metadata.
        for table, column, where, args in [
            ("metadata", "value", "key LIKE ?", ("context:" + dataset + ":%",)),
            ("answers", "value", "1=1", ()),
            ("answer_jobs", "value", "value IS NOT NULL", ()),
        ]:
            for stored in db.execute(
                f"SELECT rowid,{column} FROM {table} WHERE {where}", args
            ).fetchall():
                value = json.loads(stored[1])
                changed = self._overlay_identity(value, incoming, v)
                if changed:
                    if table in {"answers", "answer_jobs"}:
                        value["self_identity_stale"] = True
                    db.execute(
                        f"UPDATE {table} SET {column}=? WHERE rowid=?",
                        (packed(value), stored[0]),
                    )

    @classmethod
    def _overlay_identity(cls, value, record, identity):
        changed = False
        if isinstance(value, dict):
            if (
                value.get("record_id") == record["record_id"]
                and value.get("record_revision") == record["record_revision"]
                and all(
                    value.get(k) == record[k]
                    for k in ("platform", "source_account_id", "conversation_id")
                )
            ):
                changed = any(
                    k not in value or value[k] != v for k, v in identity.items()
                )
                value.update(identity)
            for child in value.values():
                changed = cls._overlay_identity(child, record, identity) or changed
        elif isinstance(value, list):
            for child in value:
                changed = cls._overlay_identity(child, record, identity) or changed
        return changed
