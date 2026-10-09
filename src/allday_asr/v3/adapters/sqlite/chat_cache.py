"""Private cache schema 1, separate from recording truth and user decisions."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from allday_asr.v3.domain.chat_data import packed, scope_key
from allday_asr.v3.ports.chat_data import ChatDataError


class ChatCache:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] not in {0, 1}:
                raise ChatDataError("CHAT_CACHE_SCHEMA_UNSUPPORTED")
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS records(
                    dataset TEXT, id TEXT, revision INTEGER NOT NULL, deleted INTEGER NOT NULL,
                    body TEXT NOT NULL, PRIMARY KEY(dataset,id));
                CREATE TABLE IF NOT EXISTS tasks(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, dataset TEXT, record_id TEXT,
                    revision INTEGER, operation TEXT, state TEXT NOT NULL DEFAULT 'pending',
                    error TEXT, UNIQUE(dataset,record_id,revision,operation));
                CREATE TABLE IF NOT EXISTS search_index(
                    dataset TEXT, id TEXT, revision INTEGER, platform TEXT, source TEXT,
                    conversation TEXT, sender TEXT, sent_at INTEGER, text TEXT, body TEXT,
                    PRIMARY KEY(dataset,id));
                CREATE INDEX IF NOT EXISTS chat_scope_search ON search_index(
                    dataset,platform,source,conversation,sender,sent_at);
                CREATE TABLE IF NOT EXISTS scopes(
                    id TEXT PRIMARY KEY, dataset TEXT NOT NULL, scope TEXT NOT NULL,
                    snapshot TEXT, boundary TEXT, page_cursor TEXT, receive_cursor TEXT,
                    phase TEXT NOT NULL, received INTEGER NOT NULL DEFAULT 0,
                    error TEXT, updated_at REAL NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS scope_records(
                    scope_id TEXT, record_id TEXT, PRIMARY KEY(scope_id,record_id));
                CREATE TABLE IF NOT EXISTS answers(
                    id TEXT PRIMARY KEY, value TEXT NOT NULL, generation INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS answer_jobs(
                    id TEXT PRIMARY KEY, state TEXT NOT NULL, value TEXT, error TEXT);
                CREATE TABLE IF NOT EXISTS user_account_links(
                    platform TEXT, source TEXT, account TEXT, person_id TEXT,
                    PRIMARY KEY(platform,source,account));
                PRAGMA user_version=1;
            """)
            db.execute(
                "UPDATE answer_jobs SET state='interrupted', error='RESTARTED' WHERE state IN ('pending','running')"
            )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        deadline = time.monotonic() + 10
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        try:
            with db:
                yield db
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc):
                raise ChatDataError("QUERY_TIMEOUT") from None
            if "locked" in str(exc):
                raise ChatDataError("SERVICE_BUSY") from None
            raise
        finally:
            db.close()

    def get(self, key, default=None):
        with self.connect() as db:
            row = db.execute(
                "SELECT value FROM metadata WHERE key=?", (key,)
            ).fetchone()
            return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.connect() as db:
            self._put(db, key, value)

    @staticmethod
    def _put(db, key, value):
        db.execute(
            "INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, packed(value)),
        )

    def activate(self, dataset):
        if not isinstance(dataset, str) or not dataset:
            raise ChatDataError("INVALID_CHAT_RESPONSE", 502)
        with self.connect() as db:
            old = db.execute(
                "SELECT value FROM metadata WHERE key='dataset'"
            ).fetchone()
            if old and json.loads(old[0]) != dataset:
                generation = db.execute(
                    "SELECT value FROM metadata WHERE key='generation'"
                ).fetchone()
                self._put(db, "generation", int(generation[0]) + 1 if generation else 1)
            self._put(db, "dataset", dataset)

    @staticmethod
    def _record(record):
        keys = {
            "record_id",
            "record_revision",
            "platform",
            "source_account_id",
            "conversation_id",
            "sender_account_id",
            "sent_at",
            "text",
            "source_locator",
        }
        if (
            not isinstance(record, dict)
            or not keys <= record.keys()
            or not isinstance(record["record_id"], str)
            or type(record["record_revision"]) is not int
            or record["record_revision"] < 1
            or record["platform"] not in {"qq", "wechat"}
            or not isinstance(record["source_account_id"], str)
            or not isinstance(record["conversation_id"], str)
            or (record["text"] is not None and not isinstance(record["text"], str))
            or (record["sent_at"] is not None and type(record["sent_at"]) is not int)
        ):
            raise ChatDataError("INVALID_CHAT_RECORD", 502)

    def receive(self, dataset, items, *, scope_id=None, progress=None, changes=False):
        # Body, durable work and cursor publish in ONE transaction. No memory ACK.
        with self.connect() as db:
            mutated = False
            for item in items:
                operation = item.get("operation", "insert") if changes else "insert"
                if operation not in {"insert", "update", "delete"}:
                    raise ChatDataError("UNSUPPORTED_CHANGE_OPERATION", 502)
                record = item.get("record") if changes else item
                if operation == "delete" and not record:
                    old = db.execute(
                        "SELECT body FROM records WHERE dataset=? AND id=?",
                        (dataset, item["record_id"]),
                    ).fetchone()
                    if not old:
                        raise ChatDataError("DELETE_BODY_UNAVAILABLE", 502)
                    record = json.loads(old[0])
                    record["record_revision"] = item["record_revision"]
                self._record(record)
                rid, revision = record["record_id"], record["record_revision"]
                if changes and (
                    item["record_id"] != rid or item["record_revision"] != revision
                ):
                    raise ChatDataError("CHANGE_REVISION_MISMATCH", 502)
                deleted = int(operation == "delete")
                old = db.execute(
                    "SELECT revision,deleted,body FROM records WHERE dataset=? AND id=?",
                    (dataset, rid),
                ).fetchone()
                body = packed(record)
                if (
                    old
                    and revision == old["revision"]
                    and (old["body"] != body or old["deleted"] != deleted)
                ):
                    raise ChatDataError("IMMUTABLE_REVISION_CHANGED", 502)
                if not old or revision > old["revision"]:
                    db.execute(
                        "INSERT INTO records VALUES(?,?,?,?,?) ON CONFLICT(dataset,id) DO UPDATE SET revision=excluded.revision,deleted=excluded.deleted,body=excluded.body",
                        (dataset, rid, revision, deleted, body),
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO tasks(dataset,record_id,revision,operation) VALUES(?,?,?,?)",
                        (dataset, rid, revision, operation),
                    )
                    # Remove stale evidence immediately, even if index work fails later.
                    db.execute(
                        "DELETE FROM search_index WHERE dataset=? AND id=?",
                        (dataset, rid),
                    )
                    mutated = True
                if scope_id:
                    db.execute(
                        "INSERT OR IGNORE INTO scope_records VALUES(?,?)",
                        (scope_id, rid),
                    )
            if mutated:
                row = db.execute(
                    "SELECT value FROM metadata WHERE key='generation'"
                ).fetchone()
                self._put(db, "generation", int(row[0]) + 1 if row else 1)
            if progress is not None:
                db.execute(
                    "UPDATE scopes SET page_cursor=?,receive_cursor=?,phase=?,error=NULL,updated_at=?,received=(SELECT count(*) FROM scope_records WHERE scope_id=?) WHERE id=?",
                    (
                        progress["page_cursor"],
                        progress["receive_cursor"],
                        progress["phase"],
                        time.time(),
                        scope_id,
                        scope_id,
                    ),
                )

    def create_scope(self, dataset, scope, snapshot):
        key = scope_key({"dataset": dataset, "scope": scope})
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO scopes(id,dataset,scope,snapshot,boundary,phase,updated_at) VALUES(?,?,?,?,?,?,?)",
                (
                    key,
                    dataset,
                    packed(scope),
                    snapshot["snapshot_id"],
                    snapshot["boundary_cursor"],
                    "snapshot",
                    time.time(),
                ),
            )
        return key

    def reset_scope(self, key):
        with self.connect() as db:
            db.execute("DELETE FROM scopes WHERE id=?", (key,))
            db.execute("DELETE FROM scope_records WHERE scope_id=?", (key,))

    def enable_scope(self, key, enabled):
        with self.connect() as db:
            db.execute("UPDATE scopes SET enabled=? WHERE id=?", (int(enabled), key))

    def scopes(self):
        with self.connect() as db:
            return [
                dict(row) | {"scope": json.loads(row["scope"])}
                for row in db.execute("SELECT * FROM scopes ORDER BY updated_at DESC")
            ]

    def scope(self, key):
        return next((row for row in self.scopes() if row["id"] == key), None)

    def scope_error(self, key, code):
        with self.connect() as db:
            db.execute(
                "UPDATE scopes SET error=?,updated_at=? WHERE id=?",
                (code, time.time(), key),
            )

    def process(self, limit=100, fail=None):
        with self.connect() as db:
            tasks = db.execute(
                "SELECT * FROM tasks WHERE state='pending' ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        for task in tasks:
            try:
                with self.connect() as db:
                    if fail:
                        fail(task)
                    row = db.execute(
                        "SELECT * FROM records WHERE dataset=? AND id=?",
                        (task["dataset"], task["record_id"]),
                    ).fetchone()
                    if row and row["revision"] == task["revision"]:
                        record = json.loads(row["body"])
                        if not row["deleted"]:
                            db.execute(
                                "INSERT INTO search_index VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(dataset,id) DO UPDATE SET revision=excluded.revision,platform=excluded.platform,source=excluded.source,conversation=excluded.conversation,sender=excluded.sender,sent_at=excluded.sent_at,text=excluded.text,body=excluded.body",
                                (
                                    task["dataset"],
                                    record["record_id"],
                                    record["record_revision"],
                                    record["platform"],
                                    record["source_account_id"],
                                    record["conversation_id"],
                                    record["sender_account_id"],
                                    record["sent_at"],
                                    record["text"],
                                    row["body"],
                                ),
                            )
                    db.execute(
                        "UPDATE tasks SET state='done',error=NULL WHERE id=?",
                        (task["id"],),
                    )
            except Exception:
                with self.connect() as db:
                    db.execute(
                        "UPDATE tasks SET state='failed',error='INDEX_FAILED' WHERE id=?",
                        (task["id"],),
                    )
        return len(tasks)

    def retry_index(self):
        with self.connect() as db:
            db.execute(
                "UPDATE tasks SET state='pending',error=NULL WHERE state='failed'"
            )

    def health(self):
        with self.connect() as db:
            counts = {
                row[0]: row[1]
                for row in db.execute("SELECT state,count(*) FROM tasks GROUP BY state")
            }
            gap = db.execute(
                "SELECT min(id) FROM tasks WHERE state!='done'"
            ).fetchone()[0]
            water = (
                gap - 1
                if gap
                else db.execute("SELECT coalesce(max(id),0) FROM tasks").fetchone()[0]
            )
            return {
                "received": db.execute("SELECT count(*) FROM records").fetchone()[0],
                "pending": counts.get("pending", 0),
                "failed": counts.get("failed", 0),
                "indexed": counts.get("done", 0),
                "processing_watermark": water,
                "model_analyzed_messages": 0,
                "model_analysis": "on_demand_only",
            }

    def search(self, scope, limit=50, cursor=None):
        offset = 0
        identity = scope_key(
            {
                "scope": scope,
                "dataset": self.get("dataset"),
                "generation": self.get("generation", 0),
            }
        )
        if cursor:
            try:
                cid, raw = cursor.split(":")
                if cid != identity:
                    raise ChatDataError("CACHE_CURSOR_EXPIRED", 410)
                offset = int(raw)
                if offset < 0:
                    raise ValueError()
            except ValueError:
                raise ChatDataError("INVALID_CURSOR", 400) from None
        clauses, params = ["dataset=?"], [self.get("dataset", "")]
        for key, column in [
            ("platform", "platform"),
            ("source_account_id", "source"),
            ("conversation_id", "conversation"),
            ("sender_account_id", "sender"),
        ]:
            if key in scope:
                clauses.append(column + "=?")
                params.append(scope[key])
        for key, op in [("sent_from", ">="), ("sent_to", "<=")]:
            if key in scope:
                clauses.append("sent_at" + op + "?")
                params.append(scope[key])
        if "keyword" in scope:
            clauses.append("instr(coalesce(text,''),?)>0")
            params.append(scope["keyword"])
        sql = (
            "SELECT body FROM search_index WHERE "
            + " AND ".join(clauses)
            + " ORDER BY sent_at,id LIMIT ? OFFSET ?"
        )
        with self.connect() as db:
            rows = db.execute(sql, (*params, limit + 1, offset)).fetchall()
        return {
            "items": [json.loads(row[0]) for row in rows[:limit]],
            "has_more": len(rows) > limit,
            "next_cursor": identity + ":" + str(offset + limit)
            if len(rows) > limit
            else None,
            "scope": scope,
            "origin": "cache",
            "order": "sent_at,id",
            "complete": False,
        }

    def record(self, rid):
        with self.connect() as db:
            row = db.execute(
                "SELECT body FROM records WHERE dataset=? AND id=? AND deleted=0",
                (self.get("dataset", ""), rid),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def context(self, rid, before, after):
        anchor = self.record(rid)
        if not anchor:
            raise ChatDataError("RECORD_NOT_FOUND", 404)
        params = (
            self.get("dataset", ""),
            anchor["platform"],
            anchor["source_account_id"],
            anchor["conversation_id"],
        )
        base = "SELECT body FROM search_index WHERE dataset=? AND platform=? AND source=? AND conversation=?"
        stamp = anchor["sent_at"] if anchor["sent_at"] is not None else -1
        with self.connect() as db:
            previous = db.execute(
                base
                + " AND (coalesce(sent_at,-1),id)<(?,?) ORDER BY coalesce(sent_at,-1) DESC,id DESC LIMIT ?",
                (*params, stamp, rid, before + 1),
            ).fetchall()
            following = db.execute(
                base
                + " AND (coalesce(sent_at,-1),id)>(?,?) ORDER BY coalesce(sent_at,-1),id LIMIT ?",
                (*params, stamp, rid, after + 1),
            ).fetchall()
        records = (
            [json.loads(row[0]) for row in reversed(previous[:before])]
            + [anchor]
            + [json.loads(row[0]) for row in following[:after]]
        )
        return {
            "items": records,
            "anchor_record_id": rid,
            "references": [],
            "reference_status": "CACHE_REFERENCE_NOT_VERIFIED",
            "truncated_before": True,
            "truncated_after": True,
            "origin": "cache",
            "complete": False,
        }

    def job(self, jid, state=None, value=None, error=None):
        with self.connect() as db:
            if state:
                db.execute(
                    "INSERT INTO answer_jobs VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,value=excluded.value,error=excluded.error",
                    (jid, state, packed(value) if value is not None else None, error),
                )
            row = db.execute("SELECT * FROM answer_jobs WHERE id=?", (jid,)).fetchone()
            if not row:
                raise ChatDataError("CHAT_JOB_NOT_FOUND", 404)
            return dict(row) | {
                "value": json.loads(row["value"]) if row["value"] else None
            }
