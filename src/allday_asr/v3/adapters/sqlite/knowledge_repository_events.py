from __future__ import annotations

from typing import Any

from allday_asr.v3.domain.knowledge import (
    EventCurrentState,
    EventOperation,
)
from .knowledge_repository_codec import (
    _datetime,
    _dict,
    _event_state,
    _json,
)


class KnowledgeEventRepositoryMixin:
    def utterance_revisions(
        self, utterance_ids: tuple[str, ...]
    ) -> dict[str, tuple[str, int]]:
        if not utterance_ids:
            return {}
        placeholders = ",".join("?" for _ in utterance_ids)
        rows = self.connection.execute(
            f"""
            SELECT utterance_id, session_id, revision FROM utterances
            WHERE utterance_id IN ({placeholders}) AND status = 'active'
            """,
            utterance_ids,
        ).fetchall()
        return {
            str(row["utterance_id"]): (str(row["session_id"]), int(row["revision"]))
            for row in rows
        }

    def utterance_facts(
        self, utterance_ids: tuple[str, ...]
    ) -> dict[str, dict[str, Any]]:
        if not utterance_ids:
            return {}
        placeholders = ",".join("?" for _ in utterance_ids)
        rows = self.connection.execute(
            f"""
            SELECT utterance_id, session_id, revision, identity, status
            FROM utterances WHERE utterance_id IN ({placeholders})
            """,
            utterance_ids,
        ).fetchall()
        return {str(row["utterance_id"]): _dict(row) for row in rows}

    def get_event(self, event_id: str) -> EventCurrentState | None:
        row = self.connection.execute(
            """
            SELECT state.*, CASE WHEN EXISTS (
              SELECT 1 FROM invalidation_events invalidation
              WHERE invalidation.target_type = 'event'
                AND invalidation.target_id = state.event_id
                AND invalidation.target_revision = state.revision
                AND invalidation.status IN ('stale', 'invalid')
            ) THEN 'stale' ELSE 'active' END AS derivation_status
            FROM event_current_states state WHERE state.event_id = ?
            """,
            (event_id,),
        ).fetchone()
        return _event_state(row) if row is not None else None

    def add_event_operation(self, operation: EventOperation) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO event_operations (
              operation_id, event_id, session_id, event_kind, operation_kind,
              event_revision, payload_json, actor, generation_id, proposal_id,
              created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                operation.operation_id,
                operation.event_id,
                operation.session_id,
                operation.event_kind.value,
                operation.operation.value,
                operation.event_revision,
                _json(operation.payload),
                operation.actor,
                operation.generation_id,
                operation.proposal_id,
                _datetime(operation.created_at),
            ),
        )
        return cursor.rowcount == 1

    def put_event_state(self, state: EventCurrentState, expected_revision: int) -> None:
        values = (
            state.session_id,
            state.event_kind.value,
            state.status.value,
            state.revision,
            _json(state.payload),
            state.latest_operation_id,
            _datetime(state.updated_at),
        )
        if expected_revision == 0:
            cursor = self.connection.execute(
                """
                INSERT INTO event_current_states (
                  event_id, session_id, event_kind, status, revision,
                  payload_json, latest_operation_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    state.event_id,
                    state.session_id,
                    state.event_kind.value,
                    state.status.value,
                    state.revision,
                    _json(state.payload),
                    state.latest_operation_id,
                    _datetime(state.created_at),
                    _datetime(state.updated_at),
                ),
            )
        else:
            cursor = self.connection.execute(
                """
                UPDATE event_current_states SET session_id = ?, event_kind = ?,
                  status = ?, revision = ?, payload_json = ?,
                  latest_operation_id = ?, updated_at = ?
                WHERE event_id = ? AND revision = ?
                """,
                (*values, state.event_id, expected_revision),
            )
        if cursor.rowcount != 1:
            raise ValueError("event revision conflict")

    def list_events(self, session_id: str) -> tuple[dict[str, Any], ...]:
        rows = self.connection.execute(
            """
            SELECT state.*, CASE WHEN EXISTS (
              SELECT 1 FROM invalidation_events invalidation
              WHERE invalidation.target_type = 'event'
                AND invalidation.target_id = state.event_id
                AND invalidation.target_revision = state.revision
                AND invalidation.status IN ('stale', 'invalid')
            ) THEN 'stale' ELSE 'active' END AS derivation_status
            FROM event_current_states state WHERE state.session_id = ?
            ORDER BY state.updated_at DESC, state.event_id
            """,
            (session_id,),
        ).fetchall()
        return tuple(self._event_dict(row) for row in rows)

    def event_history(self, event_id: str) -> tuple[dict[str, Any], ...]:
        rows = self.connection.execute(
            """
            SELECT * FROM event_operations WHERE event_id = ?
            ORDER BY event_revision, operation_id
            """,
            (event_id,),
        ).fetchall()
        if not rows:
            raise KeyError(f"event does not exist: {event_id}")
        return tuple(self._operation_dict(row) for row in rows)
