from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from allday_asr.v3.domain.ids import new_ulid

from .person_memory_repository_codec import (
    _contains_reference,
    _digest,
    _json,
    _row,
    _unique_text,
)


class PersonMemoryRecordRepositoryMixin:
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
