from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from collections.abc import Sequence
from typing import Any

from allday_asr.v3.domain.ids import new_ulid


class SqlitePersonMemoryRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def profile(self, person_id: str) -> dict[str, Any]:
        person = self.connection.execute(
            """
            SELECT p.*,
              (SELECT COUNT(*) FROM person_cluster_links link
                WHERE link.person_id = p.person_id AND link.status = 'active')
                AS cluster_count,
              (SELECT COUNT(*) FROM voice_prototypes prototype
                WHERE prototype.person_id = p.person_id
                  AND prototype.status = 'accepted'
                  AND EXISTS (
                    SELECT 1 FROM person_cluster_links prototype_link
                    WHERE prototype_link.cluster_id = prototype.cluster_id
                      AND prototype_link.person_id = prototype.person_id
                      AND prototype_link.status = 'active'
                  )
                  AND COALESCE(
                    (SELECT review.decision
                     FROM voice_prototype_reviews review
                     WHERE review.prototype_id = COALESCE(
                         prototype.source_prototype_id, prototype.prototype_id
                       )
                       AND review.person_id = prototype.person_id
                     ORDER BY review.created_at DESC, review.review_id DESC
                     LIMIT 1),
                    'confirmed'
                  ) = 'confirmed') AS prototype_count
            FROM persons p WHERE p.person_id = ?
            """,
            (person_id,),
        ).fetchone()
        if person is None:
            raise KeyError(f"active person does not exist: {person_id}")
        profile = self.connection.execute(
            """
            SELECT * FROM person_profile_revisions WHERE person_id = ?
            ORDER BY revision DESC LIMIT 1
            """,
            (person_id,),
        ).fetchone()
        value = _row(person)
        if profile is None:
            value.update({"aliases": [], "relationship_labels": [], "notes": ""})
        else:
            value.update(
                {
                    "profile_revision": int(profile["revision"]),
                    "display_name": str(profile["display_name"]),
                    "aliases": json.loads(profile["aliases_json"]),
                    "relationship_labels": json.loads(
                        profile["relationship_labels_json"]
                    ),
                    "notes": str(profile["notes"]),
                }
            )
        return value

    def update_profile(
        self,
        person_id: str,
        display_name: str,
        aliases: Sequence[str],
        relationship_labels: Sequence[str],
        notes: str,
        actor: str,
        created_at: str,
    ) -> dict[str, Any]:
        current = self.profile(person_id)
        revision = int(current.get("profile_revision", 0)) + 1
        clean_aliases = _unique_text(aliases)
        clean_labels = _unique_text(relationship_labels)
        self.connection.execute(
            """
            INSERT INTO person_profile_revisions (
              person_id, revision, display_name, aliases_json,
              relationship_labels_json, notes, actor, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                person_id,
                revision,
                display_name,
                _json(clean_aliases),
                _json(clean_labels),
                notes.strip(),
                actor,
                created_at,
            ),
        )
        self.connection.execute(
            """
            UPDATE persons SET display_name = ?, revision = revision + 1, updated_at = ?
            WHERE person_id = ?
            """,
            (display_name, created_at, person_id),
        )
        return self.profile(person_id)

    def event_sources(self, person_id: str) -> tuple[dict[str, Any], ...]:
        self.profile(person_id)
        rows = self.connection.execute(
            """
            SELECT e.*, o.actor, s.captured_start,
              reminder.status AS reminder_status,
              reminder.scheduled_at AS reminder_scheduled_at
            FROM event_current_states e
            JOIN event_operations o ON o.operation_id = e.latest_operation_id
            JOIN recording_sessions s ON s.session_id = e.session_id
            LEFT JOIN reminder_schedules reminder ON reminder.event_id = e.event_id
            WHERE instr(e.payload_json, ?) > 0
            ORDER BY e.updated_at, e.event_id
            """,
            (person_id,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            if not _contains_reference(payload, person_id):
                continue
            evidence = self.connection.execute(
                """
                SELECT DISTINCT evidence_id FROM evidence_links
                WHERE subject_type = 'event' AND subject_id = ?
                  AND subject_revision <= ? AND evidence_type = 'utterance'
                ORDER BY evidence_id
                """,
                (row["event_id"], row["revision"]),
            ).fetchall()
            result.append(
                {
                    **_row(row),
                    "payload": payload,
                    "evidence_utterance_ids": tuple(
                        str(item["evidence_id"]) for item in evidence
                    ),
                }
            )
        return tuple(result)

    def current_memory(self, memory_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT * FROM person_memory_entries WHERE memory_id = ?
            ORDER BY revision DESC LIMIT 1
            """,
            (memory_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"person memory does not exist: {memory_id}")
        return self._memory(row)

    def add_revision(
        self,
        *,
        memory_id: str,
        person_id: str,
        kind: str,
        summary: str,
        details: dict[str, Any],
        source: str,
        confidence: float,
        confirmation_status: str,
        valid_from: str,
        valid_until: str | None,
        status: str,
        event_id: str | None,
        reminder_event_id: str | None,
        evidence_utterance_ids: Sequence[str],
        operation_id: str,
        operation_kind: str,
        actor: str,
        created_at: str,
        operation_payload: dict[str, Any] | None = None,
        reverts_operation_id: str | None = None,
    ) -> dict[str, Any]:
        self.profile(person_id)
        revision_row = self.connection.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM person_memory_entries WHERE memory_id = ?",
            (memory_id,),
        ).fetchone()
        revision = int(revision_row[0])
        event_revision: int | None = None
        linked_utterances = tuple(dict.fromkeys(evidence_utterance_ids))
        if event_id is not None:
            event = self.connection.execute(
                "SELECT revision FROM event_current_states WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if event is None:
                raise KeyError(f"person memory event does not exist: {event_id}")
            event_revision = int(event["revision"])
            if not linked_utterances:
                rows = self.connection.execute(
                    """
                    SELECT DISTINCT evidence_id FROM evidence_links
                    WHERE subject_type = 'event' AND subject_id = ?
                      AND subject_revision <= ? AND evidence_type = 'utterance'
                    ORDER BY evidence_id
                    """,
                    (event_id, event_revision),
                ).fetchall()
                linked_utterances = tuple(str(row["evidence_id"]) for row in rows)
        if event_id is None and not linked_utterances:
            raise ValueError("person memory requires event or utterance evidence")
        self._validate_utterances(linked_utterances)
        if reminder_event_id is not None:
            reminder = self.connection.execute(
                "SELECT 1 FROM reminder_schedules WHERE event_id = ?",
                (reminder_event_id,),
            ).fetchone()
            if reminder is None:
                raise KeyError(
                    f"person memory reminder does not exist: {reminder_event_id}"
                )
        content_sha256 = _digest(
            {
                "person_id": person_id,
                "kind": kind,
                "summary": summary,
                "details": details,
                "source": source,
                "confidence": confidence,
                "confirmation_status": confirmation_status,
                "valid_from": valid_from,
                "valid_until": valid_until,
                "status": status,
                "event_id": event_id,
                "reminder_event_id": reminder_event_id,
                "evidence_utterance_ids": linked_utterances,
            }
        )
        self.connection.execute(
            """
            INSERT INTO person_memory_entries (
              memory_id, revision, person_id, kind, summary, details_json, source,
              confidence, confirmation_status, valid_from, valid_until, status,
              event_id, reminder_event_id, content_sha256, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                revision,
                person_id,
                kind,
                summary.strip(),
                _json(details),
                source,
                confidence,
                confirmation_status,
                valid_from,
                valid_until,
                status,
                event_id,
                reminder_event_id,
                content_sha256,
                actor,
                created_at,
            ),
        )
        if event_id is not None:
            self.connection.execute(
                """
                INSERT INTO person_memory_evidence (
                  link_id, memory_id, memory_revision, event_id,
                  event_revision, utterance_id, created_at
                ) VALUES (?, ?, ?, ?, ?, NULL, ?)
                """,
                (new_ulid(), memory_id, revision, event_id, event_revision, created_at),
            )
        for utterance_id in linked_utterances:
            self.connection.execute(
                """
                INSERT INTO person_memory_evidence (
                  link_id, memory_id, memory_revision, event_id,
                  event_revision, utterance_id, created_at
                ) VALUES (?, ?, ?, NULL, NULL, ?, ?)
                """,
                (new_ulid(), memory_id, revision, utterance_id, created_at),
            )
        self.connection.execute(
            """
            INSERT INTO person_memory_operations (
              operation_id, memory_id, memory_revision, kind, actor,
              payload_json, reverts_operation_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                operation_id,
                memory_id,
                revision,
                operation_kind,
                actor,
                _json(operation_payload or {}),
                reverts_operation_id,
                created_at,
            ),
        )
        return self.current_memory(memory_id)

    def person_detail(self, person_id: str, limit: int = 200) -> dict[str, Any]:
        profile = self.profile(person_id)
        memories = self.list_current(person_id, limit=limit, include_inactive=True)
        interactions = self._interactions(person_id, limit)
        first = min((item["occurred_at"] for item in interactions), default=None)
        latest = max((item["occurred_at"] for item in interactions), default=None)
        topics: Counter[str] = Counter()
        for memory in memories:
            if memory["status"] != "active":
                continue
            for topic in memory["details"].get("topics", []):
                if isinstance(topic, str) and topic.strip():
                    topics[topic.strip()] += 1
        commitments = [
            memory
            for memory in memories
            if memory["kind"] == "commitment" and memory["status"] == "active"
        ]
        return {
            **profile,
            "first_seen_at": first,
            "last_seen_at": latest,
            "interaction_count": len(interactions),
            "memory_count": sum(item["status"] == "active" for item in memories),
            "interactions": interactions,
            "memories": memories,
            "commitments": commitments,
            "topics": [
                {"label": label, "count": count}
                for label, count in topics.most_common(20)
            ],
        }

    def list_current(
        self, person_id: str, *, limit: int, include_inactive: bool
    ) -> tuple[dict[str, Any], ...]:
        self.profile(person_id)
        status = "" if include_inactive else "AND current.status = 'active'"
        rows = self.connection.execute(
            f"""
            SELECT current.* FROM person_memory_entries current
            JOIN (
              SELECT memory_id, MAX(revision) AS revision
              FROM person_memory_entries GROUP BY memory_id
            ) latest ON latest.memory_id = current.memory_id
              AND latest.revision = current.revision
            WHERE current.person_id = ? {status}
            ORDER BY current.valid_from DESC, current.created_at DESC,
              current.memory_id DESC LIMIT ?
            """,
            (person_id, limit),
        ).fetchall()
        return tuple(self._memory(row) for row in rows)

    def summary_counts(self) -> dict[str, dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT p.person_id,
              COUNT(DISTINCT CASE WHEN m.status = 'active' THEN m.memory_id END)
                AS memory_count,
              COUNT(DISTINCT s.session_id) AS encounter_count,
              MAX(s.captured_start) AS last_encounter_at
            FROM persons p
            LEFT JOIN (
              SELECT value.* FROM person_memory_entries value
              JOIN (
                SELECT memory_id, MAX(revision) AS revision
                FROM person_memory_entries GROUP BY memory_id
              ) latest ON latest.memory_id = value.memory_id
                AND latest.revision = value.revision
            ) m ON m.person_id = p.person_id
            LEFT JOIN person_cluster_links l
              ON l.person_id = p.person_id AND l.status = 'active'
            LEFT JOIN speaker_cluster_memberships membership
              ON membership.cluster_id = l.cluster_id AND membership.state = 'active'
            LEFT JOIN speaker_tracks track
              ON track.speaker_track_id = membership.speaker_track_id
            LEFT JOIN recording_sessions s ON s.session_id = track.session_id
            GROUP BY p.person_id
            """
        ).fetchall()
        events = self.connection.execute(
            """
            SELECT payload_json, updated_at
            FROM event_current_states
            ORDER BY event_id
            """
        ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            person_id = str(row["person_id"])
            event_count = 0
            last_event_at: str | None = None
            for event in events:
                if not _contains_reference(
                    json.loads(event["payload_json"]), person_id
                ):
                    continue
                event_count += 1
                updated_at = str(event["updated_at"])
                if last_event_at is None or updated_at > last_event_at:
                    last_event_at = updated_at
            last_interaction_at = max(
                (
                    value
                    for value in (row["last_encounter_at"], last_event_at)
                    if value is not None
                ),
                default=None,
            )
            result[person_id] = {
                "memory_count": int(row["memory_count"]),
                "interaction_count": int(row["encounter_count"]) + event_count,
                "last_interaction_at": last_interaction_at,
            }
        return result

    def revise_status(
        self,
        memory_id: str,
        status: str,
        actor: str,
        operation_id: str,
        operation_kind: str,
        created_at: str,
    ) -> dict[str, Any]:
        current = self.current_memory(memory_id)
        evidence = tuple(
            str(item["utterance_id"])
            for item in current["evidence"]
            if item.get("utterance_id") is not None
        )
        return self.add_revision(
            memory_id=memory_id,
            person_id=str(current["person_id"]),
            kind=str(current["kind"]),
            summary=str(current["summary"]),
            details=dict(current["details"]),
            source=str(current["source"]),
            confidence=float(current["confidence"]),
            confirmation_status=str(current["confirmation_status"]),
            valid_from=str(current["valid_from"]),
            valid_until=(created_at if status == "expired" else current["valid_until"]),
            status=status,
            event_id=current["event_id"],
            reminder_event_id=current["reminder_event_id"],
            evidence_utterance_ids=evidence,
            operation_id=operation_id,
            operation_kind=operation_kind,
            actor=actor,
            created_at=created_at,
            operation_payload={"previous_revision": current["revision"]},
        )

    def undo(
        self, memory_id: str, actor: str, operation_id: str, created_at: str
    ) -> dict[str, Any]:
        operation = self.connection.execute(
            """
            SELECT value.* FROM person_memory_operations value
            WHERE value.memory_id = ?
              AND value.kind NOT IN ('restore', 'create', 'project')
              AND NOT EXISTS (
                SELECT 1 FROM person_memory_operations undo
                WHERE undo.reverts_operation_id = value.operation_id
              )
            ORDER BY value.memory_revision DESC, value.created_at DESC,
              value.operation_id DESC LIMIT 1
            """,
            (memory_id,),
        ).fetchone()
        if operation is None:
            raise ValueError("person memory has no reversible operation")
        current = self.current_memory(memory_id)
        target_revision = int(operation["memory_revision"]) - 1
        if target_revision < 1:
            source = current
            next_status = "retracted"
        else:
            row = self.connection.execute(
                "SELECT * FROM person_memory_entries WHERE memory_id = ? AND revision = ?",
                (memory_id, target_revision),
            ).fetchone()
            if row is None:
                raise ValueError("person memory previous revision is unavailable")
            source = self._memory(row)
            next_status = str(source["status"])
        evidence = tuple(
            str(item["utterance_id"])
            for item in source["evidence"]
            if item.get("utterance_id") is not None
        )
        return self.add_revision(
            memory_id=memory_id,
            person_id=str(source["person_id"]),
            kind=str(source["kind"]),
            summary=str(source["summary"]),
            details=dict(source["details"]),
            source=str(source["source"]),
            confidence=float(source["confidence"]),
            confirmation_status=str(source["confirmation_status"]),
            valid_from=str(source["valid_from"]),
            valid_until=source["valid_until"],
            status=next_status,
            event_id=source["event_id"],
            reminder_event_id=source["reminder_event_id"],
            evidence_utterance_ids=evidence,
            operation_id=operation_id,
            operation_kind="restore",
            actor=actor,
            created_at=created_at,
            operation_payload={"restored_revision": target_revision},
            reverts_operation_id=str(operation["operation_id"]),
        )

    def reconcile_identity(
        self,
        cluster_id: str,
        old_person_id: str | None,
        new_person_id: str | None,
        actor: str,
        created_at: str,
    ) -> int:
        if old_person_id is None or old_person_id == new_person_id:
            return 0
        rows = self.connection.execute(
            """
            SELECT current.* FROM person_memory_entries current
            JOIN (
              SELECT memory_id, MAX(revision) AS revision
              FROM person_memory_entries GROUP BY memory_id
            ) latest ON latest.memory_id = current.memory_id
              AND latest.revision = current.revision
            WHERE current.person_id = ? AND EXISTS (
              SELECT 1 FROM person_memory_evidence evidence
              JOIN utterances utterance
                ON utterance.utterance_id = evidence.utterance_id
              JOIN speaker_cluster_memberships membership
                ON membership.speaker_track_id = utterance.speaker_track_id
              WHERE evidence.memory_id = current.memory_id
                AND evidence.memory_revision = current.revision
                AND membership.cluster_id = ? AND membership.state = 'active'
            )
            """,
            (old_person_id, cluster_id),
        ).fetchall()
        for row in rows:
            current = self._memory(row)
            evidence = tuple(
                str(item["utterance_id"])
                for item in current["evidence"]
                if item.get("utterance_id") is not None
            )
            details = dict(current["details"])
            details["identity_reconciliation"] = {
                "cluster_id": cluster_id,
                "from_person_id": old_person_id,
                "to_person_id": new_person_id,
            }
            self.add_revision(
                memory_id=str(current["memory_id"]),
                person_id=new_person_id or old_person_id,
                kind=str(current["kind"]),
                summary=str(current["summary"]),
                details=details,
                source=str(current["source"]),
                confidence=float(current["confidence"]),
                confirmation_status=str(current["confirmation_status"]),
                valid_from=str(current["valid_from"]),
                valid_until=current["valid_until"],
                status=str(current["status"]) if new_person_id else "retracted",
                event_id=current["event_id"],
                reminder_event_id=current["reminder_event_id"],
                evidence_utterance_ids=evidence,
                operation_id=new_ulid(),
                operation_kind="identity_rebind",
                actor=actor,
                created_at=created_at,
                operation_payload=details["identity_reconciliation"],
            )
        return len(rows)

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
            """
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
            WHERE link.person_id = ? AND link.status = 'active'
            GROUP BY session.session_id
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


def _event_summary(payload: dict[str, Any], fallback: str) -> str:
    for key in ("summary", "content", "text", "description", "title"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def _contains_reference(value: object, reference_id: str) -> bool:
    if value == reference_id:
        return True
    if isinstance(value, dict):
        return any(_contains_reference(item, reference_id) for item in value.values())
    if isinstance(value, list):
        return any(_contains_reference(item, reference_id) for item in value)
    return False


def _unique_text(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _row(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {key: row[key] for key in row.keys() if not key.endswith("_json")}


__all__ = ["SqlitePersonMemoryRepository"]
