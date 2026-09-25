from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from allday_asr.v3.domain.reminders import (
    CommitmentDirection,
    ReminderCandidate,
    ReminderCandidateStatus,
    ReminderFeedback,
    ReminderOperation,
    ReminderSchedule,
    ReminderScheduleStatus,
)


from .reminder_source_repository import ReminderSourceMixin


class SqliteReminderRepository(ReminderSourceMixin):
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add_candidate(self, candidate: ReminderCandidate) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO reminder_candidates (
              candidate_id, proposal_id, generation_id, operation, session_id,
              title, actor_person_id, commitment_direction,
              related_person_ids_json, scheduled_at, location, confidence,
              needs_confirmation, target_event_id, expected_revision, dedup_key,
              status, matched_event_id, conflict_reason, created_at,
              resolved_at, resolved_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                candidate.candidate_id,
                candidate.proposal_id,
                candidate.generation_id,
                candidate.operation.value,
                candidate.session_id,
                candidate.title,
                candidate.actor_person_id,
                candidate.commitment_direction.value,
                _json(list(candidate.related_person_ids)),
                _optional_datetime(candidate.scheduled_at),
                candidate.location,
                candidate.confidence,
                int(candidate.needs_confirmation),
                candidate.target_event_id,
                candidate.expected_revision,
                candidate.dedup_key,
                candidate.status.value,
                candidate.matched_event_id,
                candidate.conflict_reason,
                _datetime(candidate.created_at),
                _optional_datetime(candidate.resolved_at),
                candidate.resolved_by,
            ),
        )
        return cursor.rowcount == 1

    def get_candidate(self, candidate_id: str) -> ReminderCandidate:
        row = self.connection.execute(
            "SELECT * FROM reminder_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"reminder candidate does not exist: {candidate_id}")
        return _candidate(row)

    def get_candidate_by_proposal(self, proposal_id: str) -> ReminderCandidate | None:
        row = self.connection.execute(
            "SELECT * FROM reminder_candidates WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        return _candidate(row) if row is not None else None

    def list_candidates(
        self, status: str | None, limit: int
    ) -> tuple[dict[str, Any], ...]:
        where = "WHERE c.status = ?" if status else ""
        parameters: tuple[object, ...] = (status, limit) if status else (limit,)
        rows = self.connection.execute(
            f"""
            SELECT c.*, p.status AS proposal_status,
              p.evidence_utterance_ids_json, p.resolution_reason,
              g.producer, g.producer_version, g.model,
              g.prompt_version, g.extractor_version
            FROM reminder_candidates c
            JOIN structured_change_proposals p ON p.proposal_id = c.proposal_id
            JOIN generation_records g ON g.generation_id = c.generation_id
            {where}
            ORDER BY c.created_at DESC, c.candidate_id LIMIT ?
            """,
            parameters,
        ).fetchall()
        return tuple(_candidate_dict(row) for row in rows)

    def duplicate_for(
        self, dedup_key: str, *, exclude_candidate_id: str | None = None
    ) -> dict[str, str] | None:
        schedule = self.connection.execute(
            """
            SELECT source_candidate_id AS candidate_id, event_id
            FROM reminder_schedules
            WHERE dedup_key = ? AND status IN ('scheduled', 'delivered')
            ORDER BY updated_at DESC, event_id LIMIT 1
            """,
            (dedup_key,),
        ).fetchone()
        if schedule is not None:
            return {
                "candidate_id": str(schedule["candidate_id"]),
                "event_id": str(schedule["event_id"]),
            }
        parameters: list[object] = [dedup_key]
        exclusion = ""
        if exclude_candidate_id is not None:
            exclusion = "AND candidate_id != ?"
            parameters.append(exclude_candidate_id)
        row = self.connection.execute(
            f"""
            SELECT candidate_id, matched_event_id AS event_id
            FROM reminder_candidates
            WHERE dedup_key = ? {exclusion}
              AND status IN ('pending_confirmation', 'auto_applied', 'confirmed')
            ORDER BY created_at, candidate_id LIMIT 1
            """,
            tuple(parameters),
        ).fetchone()
        if row is None:
            return None
        return {
            "candidate_id": str(row["candidate_id"]),
            "event_id": str(row["event_id"]) if row["event_id"] else "",
        }

    def resolve_candidate(
        self,
        candidate_id: str,
        status: str,
        matched_event_id: str | None,
        conflict_reason: str | None,
        resolved_at: str,
        resolved_by: str,
    ) -> None:
        cursor = self.connection.execute(
            """
            UPDATE reminder_candidates
            SET status = ?, matched_event_id = ?, conflict_reason = ?,
              resolved_at = ?, resolved_by = ?
            WHERE candidate_id = ? AND status = 'pending_confirmation'
            """,
            (
                status,
                matched_event_id,
                conflict_reason,
                resolved_at,
                resolved_by,
                candidate_id,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("reminder candidate is no longer pending")

    def get_schedule(self, event_id: str) -> ReminderSchedule | None:
        row = self.connection.execute(
            "SELECT * FROM reminder_schedules WHERE event_id = ?", (event_id,)
        ).fetchone()
        return _schedule(row) if row is not None else None

    def put_schedule(self, schedule: ReminderSchedule) -> None:
        current = self.get_schedule(schedule.event_id)
        values = (
            schedule.session_id,
            schedule.event_revision,
            schedule.source_candidate_id,
            schedule.title,
            schedule.actor_person_id,
            schedule.commitment_direction.value,
            _json(list(schedule.related_person_ids)),
            _datetime(schedule.scheduled_at),
            schedule.location,
            schedule.status.value,
            schedule.dedup_key,
            _optional_datetime(schedule.delivered_at),
            _datetime(schedule.updated_at),
        )
        if current is None:
            if schedule.event_revision != 1:
                raise ValueError("reminder schedule revision conflict")
            self.connection.execute(
                """
                INSERT INTO reminder_schedules (
                  session_id, event_revision, source_candidate_id, title,
                  actor_person_id, commitment_direction, related_person_ids_json,
                  scheduled_at, location, status, dedup_key, delivered_at,
                  updated_at, event_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*values, schedule.event_id, _datetime(schedule.created_at)),
            )
            return
        cursor = self.connection.execute(
            """
            UPDATE reminder_schedules
            SET session_id = ?, event_revision = ?, source_candidate_id = ?,
              title = ?, actor_person_id = ?, commitment_direction = ?,
              related_person_ids_json = ?, scheduled_at = ?, location = ?,
              status = ?, dedup_key = ?, delivered_at = ?, updated_at = ?
            WHERE event_id = ? AND event_revision = ?
            """,
            (*values, schedule.event_id, schedule.event_revision - 1),
        )
        if cursor.rowcount != 1:
            raise ValueError("reminder schedule revision conflict")

    def list_schedules(
        self,
        *,
        status: str | None,
        session_id: str | None,
        due_before: str | None,
        limit: int,
    ) -> tuple[dict[str, Any], ...]:
        clauses: list[str] = []
        parameters: list[object] = []
        if status is not None:
            clauses.append("schedule.status = ?")
            parameters.append(status)
        if session_id is not None:
            clauses.append("schedule.session_id = ?")
            parameters.append(session_id)
        if due_before is not None:
            clauses.extend(("schedule.status = 'scheduled'", "schedule.scheduled_at <= ?"))
            parameters.append(due_before)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        rows = self.connection.execute(
            f"""
            SELECT schedule.*, state.status AS event_status,
              EXISTS(SELECT 1 FROM recompute_requests request WHERE request.target_type='event'
                AND request.target_id=schedule.event_id AND request.target_revision=schedule.event_revision
                AND request.status IN ('queued','running')) AS source_review_required,
              state.payload_json AS event_payload_json
            FROM reminder_schedules schedule
            JOIN event_current_states state ON state.event_id = schedule.event_id
            {where}
            ORDER BY schedule.scheduled_at, schedule.event_id LIMIT ?
            """,
            tuple(parameters),
        ).fetchall()
        return tuple(_schedule_dict(row) for row in rows)

    def mark_delivered(self, event_id: str, delivered_at: str) -> None:
        cursor = self.connection.execute(
            """
            UPDATE reminder_schedules
            SET status = 'delivered', delivered_at = ?, updated_at = ?
            WHERE event_id = ? AND status = 'scheduled' AND scheduled_at <= ?
            """,
            (delivered_at, delivered_at, event_id, delivered_at),
        )
        if cursor.rowcount != 1:
            raise ValueError("reminder is not due for delivery")

    def mark_stale(
        self, event_id: str, event_revision: int, updated_at: str
    ) -> bool:
        cursor = self.connection.execute(
            """
            UPDATE reminder_schedules
            SET status = 'stale', updated_at = ?
            WHERE event_id = ? AND event_revision = ?
              AND status = 'scheduled'
              AND NOT EXISTS (SELECT 1 FROM reminder_candidates c
                WHERE c.candidate_id = reminder_schedules.source_candidate_id
                  AND c.status = 'confirmed')
              AND NOT EXISTS (SELECT 1 FROM reminder_feedback f
                JOIN reminder_candidates c ON c.candidate_id = f.candidate_id
                WHERE c.matched_event_id = reminder_schedules.event_id
                  AND f.action IN ('confirm', 'modify'))
            """,
            (updated_at, event_id, event_revision),
        )
        return cursor.rowcount == 1

    def add_feedback(self, feedback: ReminderFeedback) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO reminder_feedback (
              feedback_id, candidate_id, action, actor, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING
            """,
            (
                feedback.feedback_id,
                feedback.candidate_id,
                feedback.action.value,
                feedback.actor,
                _json(feedback.details),
                _datetime(feedback.created_at),
            ),
        )
        return cursor.rowcount == 1

    def list_feedback(self, candidate_id: str) -> tuple[dict[str, Any], ...]:
        rows = self.connection.execute(
            """
            SELECT * FROM reminder_feedback WHERE candidate_id = ?
            ORDER BY created_at, feedback_id
            """,
            (candidate_id,),
        ).fetchall()
        return tuple(_feedback_dict(row) for row in rows)


def _candidate(row: sqlite3.Row) -> ReminderCandidate:
    return ReminderCandidate(
        candidate_id=str(row["candidate_id"]),
        proposal_id=str(row["proposal_id"]),
        generation_id=str(row["generation_id"]),
        operation=ReminderOperation(str(row["operation"])),
        session_id=str(row["session_id"]),
        title=str(row["title"]) if row["title"] is not None else None,
        actor_person_id=str(row["actor_person_id"]),
        commitment_direction=CommitmentDirection(str(row["commitment_direction"])),
        related_person_ids=tuple(_string_list(row["related_person_ids_json"])),
        scheduled_at=_optional_parse_datetime(row["scheduled_at"]),
        location=str(row["location"]) if row["location"] is not None else None,
        confidence=float(row["confidence"]),
        needs_confirmation=bool(row["needs_confirmation"]),
        target_event_id=(
            str(row["target_event_id"]) if row["target_event_id"] is not None else None
        ),
        expected_revision=int(row["expected_revision"]),
        dedup_key=str(row["dedup_key"]),
        status=ReminderCandidateStatus(str(row["status"])),
        matched_event_id=(
            str(row["matched_event_id"]) if row["matched_event_id"] is not None else None
        ),
        conflict_reason=(
            str(row["conflict_reason"]) if row["conflict_reason"] is not None else None
        ),
        created_at=_parse_datetime(row["created_at"]),
        resolved_at=_optional_parse_datetime(row["resolved_at"]),
        resolved_by=str(row["resolved_by"]) if row["resolved_by"] else None,
    )


def _schedule(row: sqlite3.Row) -> ReminderSchedule:
    return ReminderSchedule(
        event_id=str(row["event_id"]),
        session_id=str(row["session_id"]),
        event_revision=int(row["event_revision"]),
        source_candidate_id=str(row["source_candidate_id"]),
        title=str(row["title"]),
        actor_person_id=str(row["actor_person_id"]),
        commitment_direction=CommitmentDirection(str(row["commitment_direction"])),
        related_person_ids=tuple(_string_list(row["related_person_ids_json"])),
        scheduled_at=_parse_datetime(row["scheduled_at"]),
        location=str(row["location"]) if row["location"] is not None else None,
        status=ReminderScheduleStatus(str(row["status"])),
        dedup_key=str(row["dedup_key"]),
        delivered_at=_optional_parse_datetime(row["delivered_at"]),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _candidate_dict(row: sqlite3.Row) -> dict[str, Any]:
    value = _dict(row)
    value["related_person_ids"] = _string_list(
        value.pop("related_person_ids_json")
    )
    value["evidence_utterance_ids"] = _string_list(
        value.pop("evidence_utterance_ids_json")
    )
    value["needs_confirmation"] = bool(value["needs_confirmation"])
    return value


def _schedule_dict(row: sqlite3.Row) -> dict[str, Any]:
    value = _dict(row)
    value["related_person_ids"] = _string_list(
        value.pop("related_person_ids_json")
    )
    value["event_payload"] = json.loads(str(value.pop("event_payload_json")))
    value["source_review_required"] = bool(value["source_review_required"])
    return value


def _feedback_dict(row: sqlite3.Row) -> dict[str, Any]:
    value = _dict(row)
    value["details"] = json.loads(str(value.pop("details_json")))
    return value


def _dict(row: sqlite3.Row) -> dict[str, Any]:
    return {str(key): row[key] for key in row.keys()}


def _string_list(value: object) -> list[str]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError("stored reminder string list is invalid")
    return parsed


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("reminder timestamps must be timezone-aware")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _optional_datetime(value: datetime | None) -> str | None:
    return _datetime(value) if value is not None else None


def _parse_datetime(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("stored reminder timestamp is not timezone-aware")
    return parsed.astimezone(timezone.utc)


def _optional_parse_datetime(value: object) -> datetime | None:
    return _parse_datetime(value) if value is not None else None


__all__ = ["SqliteReminderRepository"]
