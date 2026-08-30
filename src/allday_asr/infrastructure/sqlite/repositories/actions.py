from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from .types import Clock, ConnectionFactory


class ActionRepository:
    def __init__(self, connect: ConnectionFactory, *, now: Clock) -> None:
        self.connect = connect
        self._now = now

    def upsert_action_candidate(self, values: dict[str, Any]) -> sqlite3.Row:
        now = self._now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO action_candidates (
                    recording_id, candidate_key, candidate_type, status,
                    start_ms, end_ms, source_segment_ids_json, title,
                    scheduled_at, time_text, location, participants_json,
                    confidence, evidence_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_key) DO UPDATE SET
                    start_ms = excluded.start_ms,
                    end_ms = excluded.end_ms,
                    source_segment_ids_json = excluded.source_segment_ids_json,
                    title = CASE
                        WHEN action_candidates.status = 'pending' THEN excluded.title
                        ELSE action_candidates.title
                    END,
                    scheduled_at = CASE
                        WHEN action_candidates.status = 'pending' THEN excluded.scheduled_at
                        ELSE action_candidates.scheduled_at
                    END,
                    time_text = excluded.time_text,
                    location = CASE
                        WHEN action_candidates.status = 'pending' THEN excluded.location
                        ELSE action_candidates.location
                    END,
                    participants_json = excluded.participants_json,
                    confidence = excluded.confidence,
                    evidence_json = excluded.evidence_json,
                    updated_at = excluded.updated_at
                """,
                (
                    values["recording_id"],
                    values["candidate_key"],
                    values["candidate_type"],
                    values["start_ms"],
                    values["end_ms"],
                    json.dumps(values["source_segment_ids"], ensure_ascii=False),
                    values["title"],
                    values.get("scheduled_at"),
                    values.get("time_text"),
                    values.get("location"),
                    json.dumps(values.get("participants", []), ensure_ascii=False),
                    values["confidence"],
                    json.dumps(values["evidence"], ensure_ascii=False),
                    now,
                    now,
                ),
            )
            return connection.execute(
                "SELECT * FROM action_candidates WHERE candidate_key = ?",
                (values["candidate_key"],),
            ).fetchone()

    def list_action_candidates(
        self, recording_id: int, *, status: str | None = None
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM action_candidates WHERE recording_id = ?"
        params: list[Any] = [recording_id]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY start_ms, id"
        with self.connect() as connection:
            return list(connection.execute(sql, params))

    def get_action_candidate(self, candidate_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM action_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"行动候选 {candidate_id} 不存在")
        return row

    def review_action_candidate(
        self,
        candidate_id: int,
        *,
        status: str,
        title: str | None = None,
        scheduled_at: str | None = None,
        location: str | None = None,
    ) -> sqlite3.Row:
        if status not in {"pending", "confirmed", "dismissed"}:
            raise ValueError("status 只能是 pending、confirmed 或 dismissed")
        if title is not None and not title.strip():
            raise ValueError("title 不能为空")
        if scheduled_at is not None:
            try:
                datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("scheduled_at 必须是有效的 ISO 8601 时间") from exc
        updates = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, self._now()]
        if title is not None:
            updates.append("title = ?")
            params.append(title.strip())
        if scheduled_at is not None:
            updates.append("scheduled_at = ?")
            params.append(scheduled_at)
        if location is not None:
            updates.append("location = ?")
            params.append(location)
        params.append(candidate_id)
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE action_candidates SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            if cursor.rowcount != 1:
                raise KeyError(f"行动候选 {candidate_id} 不存在")
            return connection.execute(
                "SELECT * FROM action_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
