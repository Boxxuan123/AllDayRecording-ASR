from __future__ import annotations

import sqlite3
from typing import Any


from .people_repository_codec import _json


class PeopleRepositorySupportMixin:
    def _cluster_utterance_ids(self, cluster_id: str) -> tuple[str, ...]:
        rows = self.connection.execute(
            """
            SELECT u.utterance_id FROM speaker_cluster_memberships m
            JOIN utterances u ON u.speaker_track_id = m.speaker_track_id
            WHERE m.cluster_id = ? AND m.state = 'active' AND u.status = 'active'
              AND u.run_id = (
                SELECT p.run_id FROM processing_runs p
                LEFT JOIN processing_jobs j ON j.run_id = p.run_id
                WHERE p.session_id = u.session_id AND p.status = 'succeeded'
                  AND COALESCE(
                    json_extract(j.request_json, '$.admission_mode'),
                    'production'
                  ) = 'production'
                  AND EXISTS (
                    SELECT 1 FROM utterances active
                    WHERE active.run_id = p.run_id AND active.status = 'active'
                  )
                ORDER BY p.created_at DESC, p.run_id DESC
                LIMIT 1
              )
            ORDER BY u.start_at, u.utterance_id
            """,
            (cluster_id,),
        ).fetchall()
        return tuple(str(row["utterance_id"]) for row in rows)
    def _create_identity_policy(
        self, person_id: str, created_at: str, actor: str
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO person_identity_policy_revisions (
              person_id, revision, maturity_status, auto_match_enabled,
              suggest_threshold, auto_accept_threshold, minimum_margin,
              minimum_quality, calibration_json, actor, created_at
            ) VALUES (?, 1, 'seed', 0, 0.82, 0.92, 0.05, 0.50, '{}', ?, ?)
            """,
            (person_id, actor, created_at),
        )
    def _active_cluster(self, cluster_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM speaker_clusters WHERE cluster_id = ? AND status = 'active'",
            (cluster_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"active speaker cluster does not exist: {cluster_id}")
        return row
    def _linked_person(self, cluster_id: str) -> str | None:
        row = self.connection.execute(
            """
            SELECT person_id FROM person_cluster_links
            WHERE cluster_id = ? AND status = 'active'
            """,
            (cluster_id,),
        ).fetchone()
        return str(row["person_id"]) if row is not None else None
    def _add_operation(
        self,
        operation_id: str,
        kind: str,
        cluster_id: str,
        actor: str,
        payload: dict[str, Any],
        created_at: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO person_cluster_operations (
              operation_id, kind, cluster_id, actor, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (operation_id, kind, cluster_id, actor, _json(payload), created_at),
        )
