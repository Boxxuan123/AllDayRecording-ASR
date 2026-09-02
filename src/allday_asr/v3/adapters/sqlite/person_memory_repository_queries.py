from __future__ import annotations

import json
from collections import Counter
from typing import Any

from allday_asr.v3.domain.ids import new_ulid

from .person_memory_repository_codec import (
    _contains_reference,
)


class PersonMemoryQueryRepositoryMixin:
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
