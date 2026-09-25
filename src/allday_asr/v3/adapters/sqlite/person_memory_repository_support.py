from __future__ import annotations
from .sound_eligibility import confirmed_interaction

import json
import sqlite3
from collections.abc import Sequence
from typing import Any


from .person_memory_repository_codec import (
    _event_summary,
    _row,
)


class PersonMemoryRepositorySupportMixin:
    def _memory(self, row: sqlite3.Row) -> dict[str, Any]:
        value = _row(row)
        value["details"] = json.loads(row["details_json"])
        evidence = self.connection.execute(
            """
            SELECT link.event_id, link.event_revision, link.utterance_id,
              utterance.session_id, utterance.start_at, utterance.end_at,
              COALESCE(span.asset_start_ms, utterance.start_ms) AS start_ms,
              COALESCE(span.asset_end_ms, utterance.end_ms) AS end_ms,
              span.session_start_ms, span.session_end_ms,
              span.evidence_span_id, span.asset_id, utterance.text,
              asset.media_id
            FROM person_memory_evidence link
            LEFT JOIN utterances utterance ON utterance.utterance_id = link.utterance_id
            LEFT JOIN evidence_spans span ON span.utterance_id = utterance.utterance_id
            LEFT JOIN audio_assets asset ON asset.asset_id = span.asset_id
            WHERE link.memory_id = ? AND link.memory_revision = ?
            ORDER BY link.event_id, utterance.start_at, span.session_start_ms,
              span.evidence_span_id, link.link_id
            """,
            (row["memory_id"], row["revision"]),
        ).fetchall()
        value["evidence"] = [_row(item) for item in evidence]
        reversible = self.connection.execute(
            """
            SELECT value.kind FROM person_memory_operations value
            WHERE value.memory_id = ?
              AND value.kind NOT IN ('restore', 'create', 'project')
              AND NOT EXISTS (
                SELECT 1 FROM person_memory_operations undo
                WHERE undo.reverts_operation_id = value.operation_id
              )
            ORDER BY value.memory_revision DESC, value.created_at DESC,
              value.operation_id DESC LIMIT 1
            """,
            (row["memory_id"],),
        ).fetchone()
        status = str(row["status"])
        value["available_actions"] = {
            "can_revise": status == "active",
            "can_expire": status == "active",
            "can_retract": status != "retracted",
            "can_undo": reversible is not None,
        }
        reminder_event_id = row["reminder_event_id"]
        if reminder_event_id is not None:
            schedule = self.connection.execute(
                "SELECT * FROM reminder_schedules WHERE event_id = ?",
                (reminder_event_id,),
            ).fetchone()
            value["reminder"] = _row(schedule) if schedule is not None else None
        else:
            value["reminder"] = None
        return value
    def _interactions(self, person_id: str, limit: int) -> list[dict[str, Any]]:
        sessions = self.connection.execute(
            f"""
            SELECT session.session_id, session.captured_start AS occurred_at,
              COUNT(DISTINCT utterance.utterance_id) AS utterance_count,
              MIN(utterance.start_at) AS first_utterance_at,
              MAX(utterance.end_at) AS last_utterance_at,
              GROUP_CONCAT(substr(utterance.text, 1, 80), ' / ') AS transcript_preview
            FROM person_cluster_links link
            JOIN speaker_cluster_memberships membership
              ON membership.cluster_id = link.cluster_id AND membership.state = 'active'
            JOIN speaker_tracks track
              ON track.speaker_track_id = membership.speaker_track_id
            JOIN recording_sessions session ON session.session_id = track.session_id
            LEFT JOIN utterances utterance
              ON utterance.speaker_track_id = track.speaker_track_id
              AND utterance.status = 'active'
              AND {confirmed_interaction("utterance")}
            WHERE link.person_id = ? AND link.status = 'active'
            GROUP BY session.session_id HAVING COUNT(utterance.utterance_id) > 0
            ORDER BY session.captured_start DESC LIMIT ?
            """,
            (person_id, limit),
        ).fetchall()
        result = [
            {
                **_row(row),
                "interaction_type": "encounter",
                "event_id": None,
                "event_kind": None,
                "title": str(row["transcript_preview"] or "有语音互动")[:240],
            }
            for row in sessions
        ]
        for event in self.event_sources(person_id):
            payload = event["payload"]
            result.append(
                {
                    "interaction_type": "event",
                    "event_id": event["event_id"],
                    "event_kind": event["event_kind"],
                    "session_id": event["session_id"],
                    "occurred_at": event["updated_at"],
                    "title": _event_summary(payload, str(event["event_kind"])),
                    "status": event["status"],
                    "evidence_utterance_ids": list(event["evidence_utterance_ids"]),
                }
            )
        result.sort(
            key=lambda item: (str(item["occurred_at"]), str(item.get("event_id"))),
            reverse=True,
        )
        return result[:limit]
    def _validate_utterances(self, utterance_ids: Sequence[str]) -> None:
        if not utterance_ids:
            return
        rows = self.connection.execute(
            f"SELECT utterance_id FROM utterances WHERE utterance_id IN ({','.join('?' for _ in utterance_ids)})",
            tuple(utterance_ids),
        ).fetchall()
        found = {str(row["utterance_id"]) for row in rows}
        if found != set(utterance_ids):
            raise KeyError("person memory utterance evidence does not exist")
