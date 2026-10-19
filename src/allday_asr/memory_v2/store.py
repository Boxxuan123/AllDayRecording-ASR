"""Own derived storage only. Native records remain the authority."""

from dataclasses import asdict
import json
import sqlite3
from pathlib import Path
from .contracts import SourceRecord, digest


class SourceStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "sources.sqlite3"
        with self.connect() as c:
            c.executescript("""
              CREATE TABLE IF NOT EXISTS records(key TEXT PRIMARY KEY, revision INTEGER, fingerprint TEXT, body TEXT);
              CREATE TABLE IF NOT EXISTS episodes(id TEXT PRIMARY KEY, fingerprint TEXT, body TEXT, state TEXT);
              CREATE TABLE IF NOT EXISTS members(episode TEXT, key TEXT, PRIMARY KEY(episode,key));
              CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, kind TEXT, body TEXT);
            """)

    def connect(self):
        return sqlite3.connect(self.path)

    def records(self, grant=None):
        with self.connect() as c:
            rows = [
                SourceRecord(**json.loads(x[0]))
                for x in c.execute("SELECT body FROM records ORDER BY key")
            ]
        return [r for r in rows if not r.deleted and (grant is None or grant.allows(r))]

    def get(self, key):
        with self.connect() as c:
            row = c.execute("SELECT body FROM records WHERE key=?", (key,)).fetchone()
        return SourceRecord(**json.loads(row[0])) if row else None

    def update(self, records):
        changed = set()
        with self.connect() as c:
            for r in records:
                old = c.execute(
                    "SELECT revision,fingerprint FROM records WHERE key=?", (r.key,)
                ).fetchone()
                if old:
                    if r.revision < old[0]:
                        raise ValueError("REVISION_REGRESSION")
                    if r.revision == old[0] and r.fingerprint != old[1]:
                        raise ValueError("SAME_REVISION_CONTENT_CONFLICT")
                    if r.fingerprint == old[1]:
                        continue
                c.execute(
                    "INSERT OR REPLACE INTO records VALUES(?,?,?,?)",
                    (
                        r.key,
                        r.revision,
                        r.fingerprint,
                        json.dumps(asdict(r), ensure_ascii=False),
                    ),
                )
                # Mark stale BEFORE any upstream call; failures remain fail-closed.
                c.execute(
                    "UPDATE episodes SET state='stale' WHERE id IN (SELECT episode FROM members WHERE key=?)",
                    (r.key,),
                )
                changed.add(r.key)
            c.execute(
                "INSERT INTO events(kind,body) VALUES('update',?)",
                (json.dumps(sorted(changed)),),
            )
        return changed

    def build_episodes(self, max_chars=6500, max_records=48):
        """Stable two-hour buckets bound rewrite costs, but do not bound retrieval.
        Neighbour context is joined at retrieval; no daily semantic boundary.
        """
        groups = {}
        for r in self.records():
            bucket = [
                r.platform,
                r.source_account_id,
                r.conversation_id,
                r.sent_at // 7200,
            ]
            groups.setdefault(tuple(bucket), []).append(r)
        episodes = []
        for bucket, rows in sorted(groups.items()):
            rows.sort(key=lambda r: (r.sent_at, r.key))
            batches = []
            current = []
            size = 0
            for r in rows:
                # Full message text is kept in authoritative store; very long messages
                # have separate bounded projections with exact character spans.
                for start in range(0, max(1, len(r.text)), max_chars):
                    text = r.text[start : start + max_chars]
                    if current and (
                        size + len(text) > max_chars or len(current) >= max_records
                    ):
                        batches.append(current)
                        current = []
                        size = 0
                    current.append(
                        {
                            "key": r.key,
                            "revision": r.revision,
                            "start": start,
                            "end": start + len(text),
                            "text": text,
                            "sent_at": r.sent_at,
                            "speaker": r.speaker,
                            "actor": list(r.actor),
                            "self": r.is_from_self,
                            "conversation": r.conversation_name,
                        }
                    )
                    size += len(text)
            if current:
                batches.append(current)
            for number, batch in enumerate(batches):
                eid = digest([bucket, number])
                ep = {"id": eid, "records": batch, "sent_at": batch[0]["sent_at"]}
                ep["fingerprint"] = digest(ep)
                episodes.append(ep)
        return episodes

    def plan(self):
        desired = self.build_episodes()
        with self.connect() as c:
            existing = {
                x[0]: (x[1], x[2])
                for x in c.execute("SELECT id,fingerprint,state FROM episodes")
            }
            wanted = {e["id"] for e in desired}
            obsolete = sorted(set(existing) - wanted)
            for eid in obsolete:
                c.execute("UPDATE episodes SET state='stale' WHERE id=?", (eid,))
        return [
            e for e in desired if existing.get(e["id"]) != (e["fingerprint"], "ready")
        ], obsolete

    def ready(self, ep):
        with self.connect() as c:
            c.execute(
                "INSERT OR REPLACE INTO episodes VALUES(?,?,?,'ready')",
                (ep["id"], ep["fingerprint"], json.dumps(ep, ensure_ascii=False)),
            )
            c.execute("DELETE FROM members WHERE episode=?", (ep["id"],))
            c.executemany(
                "INSERT OR IGNORE INTO members VALUES(?,?)",
                [(ep["id"], r["key"]) for r in ep["records"]],
            )

    def remove_episode(self, eid):
        with self.connect() as c:
            c.execute("DELETE FROM episodes WHERE id=?", (eid,))
            c.execute("DELETE FROM members WHERE episode=?", (eid,))

    def episode(self, eid):
        with self.connect() as c:
            row = c.execute(
                "SELECT body FROM episodes WHERE id=? AND state='ready'", (eid,)
            ).fetchone()
        if not row:
            return None
        ep = json.loads(row[0])
        for m in ep["records"]:
            r = self.get(m["key"])
            if (
                not r
                or r.deleted
                or r.revision != m["revision"]
                or r.text[m["start"] : m["end"]] != m["text"]
            ):
                return None
        return ep
