"""Chat provenance and processing receipts, never a second task store."""

import json
import time
from allday_asr.v3.domain.chat_data import packed


class SqliteChatFollowupRepository:
    def __init__(self, connection):
        self.connection = connection

    def admission(self, key, dataset):
        row = self.connection.execute(
            "SELECT * FROM chat_followup_admissions WHERE source_key=? AND dataset=?",
            (key, dataset),
        ).fetchone()
        return dict(row) if row else None

    def save_admission(self, value):
        self.connection.execute(
            "INSERT INTO chat_followup_admissions VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source_key,dataset) DO UPDATE SET "
            "conversation_key=excluded.conversation_key,evidence_digest=excluded.evidence_digest,"
            "decision=excluded.decision,origin=excluded.origin,review_ref=excluded.review_ref,"
            "value_json=excluded.value_json",
            tuple(
                value[k]
                for k in (
                    "source_key",
                    "dataset",
                    "conversation_key",
                    "evidence_digest",
                    "decision",
                    "origin",
                    "review_ref",
                    "value_json",
                )
            ),
        )

    def source(self, key):
        row = self.connection.execute(
            "SELECT * FROM chat_followup_sources WHERE source_key=?", (key,)
        ).fetchone()
        return dict(row) if row else None

    def for_event(self, event_id):
        return [
            dict(r)
            for r in self.connection.execute(
                "SELECT * FROM chat_followup_sources WHERE event_id=?", (event_id,)
            )
        ]

    def save_source(self, key, value):
        self.connection.execute(
            """INSERT INTO chat_followup_sources VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(source_key) DO UPDATE SET event_id=excluded.event_id,
        latest_sent_at=excluded.latest_sent_at, human_override=excluded.human_override,
        ignored=excluded.ignored, conflict_json=excluded.conflict_json""",
            (
                key,
                value["event_id"],
                value["dataset"],
                value["conversation_key"],
                value.get("latest_sent_at"),
                int(value.get("human_override", False)),
                int(value.get("ignored", False)),
                value.get("conflict_json"),
            ),
        )

    def items(self, limit, offset=0):
        rows = self.connection.execute(
            """SELECT e.*, s.source_key,s.dataset,s.conversation_key,
        s.latest_sent_at,s.human_override,s.ignored,s.conflict_json FROM chat_followup_sources s
        JOIN event_current_states e ON e.event_id=s.event_id
        WHERE COALESCE(json_extract(e.payload_json, '$.chat_followup.personal_scope'), 1) != 0
        ORDER BY s.ignored ASC, CASE WHEN e.status='active' THEN 0 ELSE 1 END,
          s.latest_sent_at DESC,s.source_key LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        result = []
        for r in rows:
            v = dict(r)
            v["payload"] = json.loads(v.pop("payload_json"))
            v["conflict"] = (
                json.loads(v.pop("conflict_json")) if v["conflict_json"] else None
            )
            result.append(v)
        return result

    def effect_exists(self, key):
        return (
            self.connection.execute(
                "SELECT 1 FROM chat_followup_effects WHERE effect_key=?", (key,)
            ).fetchone()
            is not None
        )

    def effect(self, key, event_id, details):
        self.connection.execute(
            "INSERT INTO chat_followup_effects VALUES(?,?,?)",
            (key, event_id, packed(details)),
        )

    def job(self, job_id):
        row = self.connection.execute(
            "SELECT * FROM chat_followup_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if not row:
            raise KeyError("followup job not found")
        return {
            "id": row["job_id"],
            "state": row["state"],
            **json.loads(row["value_json"]),
        }

    def save_job(self, job_id, state, value):
        self.connection.execute(
            "INSERT INTO chat_followup_jobs VALUES(?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET state=excluded.state,value_json=excluded.value_json,updated_at=excluded.updated_at",
            (job_id, state, packed(value), time.time()),
        )

    def jobs(self):
        return [
            self.job(r[0])
            for r in self.connection.execute(
                "SELECT job_id FROM chat_followup_jobs ORDER BY updated_at DESC LIMIT 30"
            )
        ]
