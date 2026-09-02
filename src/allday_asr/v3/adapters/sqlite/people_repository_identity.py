from __future__ import annotations

import json
from typing import Any


from .people_repository_codec import _contains_reference, _row


class PeopleIdentityRepositoryMixin:
    def events_referencing(self, reference_id: str) -> tuple[dict[str, Any], ...]:
        rows = self.connection.execute(
            """
            SELECT event_id, session_id, event_kind, revision, payload_json
            FROM event_current_states WHERE instr(payload_json, ?) > 0
            ORDER BY event_id
            """,
            (reference_id,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            if _contains_reference(payload, reference_id):
                result.append({**_row(row), "payload": payload})
        return tuple(result)
    def events_by_ids(self, event_ids: tuple[str, ...]) -> tuple[dict[str, Any], ...]:
        if not event_ids:
            return ()
        rows = self.connection.execute(
            f"""
            SELECT event_id, session_id, event_kind, revision, payload_json
            FROM event_current_states
            WHERE event_id IN ({','.join('?' for _ in event_ids)})
            ORDER BY event_id
            """,
            event_ids,
        ).fetchall()
        return tuple({**_row(row), "payload": json.loads(row["payload_json"])} for row in rows)
    def cluster_evidence_ids(
        self, cluster_id: str, session_id: str | None = None
    ) -> tuple[str, ...]:
        if session_id is None:
            return self._cluster_utterance_ids(cluster_id)
        rows = self.connection.execute(
            """
            SELECT u.utterance_id FROM speaker_cluster_memberships m
            JOIN utterances u ON u.speaker_track_id = m.speaker_track_id
            WHERE m.cluster_id = ? AND m.state = 'active'
              AND u.session_id = ? AND u.status = 'active'
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
            (cluster_id, session_id),
        ).fetchall()
        return tuple(str(row["utterance_id"]) for row in rows)
    def set_cluster_suggestion(
        self,
        cluster_id: str,
        person_id: str | None,
        confidence: float | None,
        updated_at: str,
    ) -> None:
        if (person_id is None) != (confidence is None):
            raise ValueError("cluster suggestion person and confidence must be paired")
        if confidence is not None and not 0 <= confidence <= 1:
            raise ValueError("cluster suggestion confidence is invalid")
        self._active_cluster(cluster_id)
        self.connection.execute(
            """
            UPDATE speaker_clusters
            SET suggested_person_id = ?, suggestion_confidence = ?,
              revision = revision + 1, updated_at = ?
            WHERE cluster_id = ?
              AND NOT (
                suggested_person_id IS ? AND suggestion_confidence IS ?
              )
            """,
            (
                person_id,
                confidence,
                updated_at,
                cluster_id,
                person_id,
                confidence,
            ),
        )
    def record_match_decision(
        self,
        *,
        decision_id: str,
        cluster_id: str,
        prototype_id: str,
        speaker_track_id: str,
        decision_tier: str,
        candidate_person_id: str | None,
        best_score: float | None,
        second_best_score: float | None,
        score_margin: float | None,
        quality_score: float,
        policy_revision: int | None,
        policy_version: str,
        trigger: str,
        reason: str,
        created_at: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO speaker_match_decisions (
              decision_id, cluster_id, prototype_id, speaker_track_id,
              decision_tier, candidate_person_id, best_score,
              second_best_score, score_margin, quality_score, policy_revision,
              policy_version, reason, trigger, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id,
                cluster_id,
                prototype_id,
                speaker_track_id,
                decision_tier,
                candidate_person_id,
                best_score,
                second_best_score,
                score_margin,
                quality_score,
                policy_revision,
                policy_version,
                reason,
                trigger,
                created_at,
            ),
        )
