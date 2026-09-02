from __future__ import annotations

import json
from typing import Any

from allday_asr.v3.domain.people import SpeakerEmbedding

from .people_repository_codec import _cluster, _json, _row


class PeopleCatalogRepositoryMixin:
    def record_embedding(
        self,
        *,
        run_id: str,
        cluster_id: str,
        cluster_label: str,
        create_cluster: bool,
        membership_id: str,
        prototype_id: str,
        operation_id: str,
        embedding: SpeakerEmbedding,
        membership_confidence: float,
        suggested_person_id: str | None,
        suggestion_confidence: float | None,
        created_at: str,
        membership_source: str = "automatic",
        actor: str = "system:local-clustering",
    ) -> None:
        if membership_source not in {"automatic", "human"}:
            raise ValueError("speaker membership source is invalid")
        if create_cluster:
            self.connection.execute(
                """
                INSERT INTO speaker_clusters (
                  cluster_id, display_label, status, revision,
                  suggested_person_id, suggestion_confidence, created_at, updated_at
                ) VALUES (?, ?, 'active', 1, ?, ?, ?, ?)
                """,
                (
                    cluster_id,
                    cluster_label,
                    suggested_person_id,
                    suggestion_confidence,
                    created_at,
                    created_at,
                ),
            )
        elif suggested_person_id is not None:
            self.connection.execute(
                """
                UPDATE speaker_clusters
                SET suggested_person_id = ?, suggestion_confidence = ?,
                    revision = revision + 1, updated_at = ?
                WHERE cluster_id = ? AND status = 'active'
                """,
                (suggested_person_id, suggestion_confidence, created_at, cluster_id),
            )
        self.connection.execute(
            """
            INSERT INTO speaker_cluster_memberships (
              membership_id, cluster_id, speaker_track_id, state, source,
              confidence, revision, operation_id, created_at, updated_at
            ) VALUES (?, ?, ?, 'active', ?, ?, 1, ?, ?, ?)
            """,
            (
                membership_id,
                cluster_id,
                embedding.speaker_track_id,
                membership_source,
                membership_confidence,
                operation_id,
                created_at,
                created_at,
            ),
        )
        representatives = [
            {
                "media_id": clip.media_id,
                "start_ms": clip.start_ms,
                "end_ms": clip.end_ms,
                "utterance_id": clip.utterance_id,
            }
            for clip in embedding.representatives
        ]
        self.connection.execute(
            """
            INSERT INTO voice_prototypes (
              prototype_id, speaker_track_id, cluster_id, status, model,
              model_version, dimensions, vector_json, representative_clips_json,
              quality_score, human_confirmed, operation_id, created_at
            ) VALUES (?, ?, ?, 'candidate', ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                prototype_id,
                embedding.speaker_track_id,
                cluster_id,
                embedding.model,
                embedding.model_version,
                len(embedding.vector),
                _json(list(embedding.vector)),
                _json(representatives),
                embedding.quality_score,
                operation_id,
                created_at,
            ),
        )
        self.connection.execute(
            """
            INSERT INTO person_cluster_operations (
              operation_id, kind, cluster_id, actor, payload_json, created_at
            ) VALUES (?, 'analyze', ?, ?, ?, ?)
            """,
            (
                operation_id,
                cluster_id,
                actor,
                _json({"run_id": run_id, "speaker_track_id": embedding.speaker_track_id}),
                created_at,
            ),
        )
    def list_people(self) -> tuple[dict[str, Any], ...]:
        rows = self.connection.execute(
            """
            SELECT p.*,
              (SELECT COUNT(*) FROM person_cluster_links l
                WHERE l.person_id = p.person_id AND l.status = 'active') AS cluster_count,
              (SELECT COUNT(*) FROM voice_prototypes v
                WHERE v.person_id = p.person_id AND v.status = 'accepted'
                  AND COALESCE(
                    (SELECT review.decision
                     FROM voice_prototype_reviews review
                     WHERE review.prototype_id = COALESCE(
                         v.source_prototype_id, v.prototype_id
                       )
                       AND review.person_id = p.person_id
                     ORDER BY review.created_at DESC, review.review_id DESC
                     LIMIT 1),
                    'confirmed'
                  ) = 'confirmed') AS prototype_count,
              policy.revision AS voice_policy_revision,
              policy.maturity_status AS voice_maturity_status,
              policy.auto_match_enabled AS known_auto_match_enabled,
              policy.suggest_threshold, policy.auto_accept_threshold,
              policy.minimum_margin, policy.minimum_quality,
              policy.calibration_json,
              (SELECT COUNT(*) FROM voice_prototype_reviews review
                WHERE review.person_id = p.person_id
                  AND review.decision = 'rejected'
                  AND NOT EXISTS (
                    SELECT 1 FROM voice_prototype_reviews newer
                    WHERE newer.prototype_id = review.prototype_id
                      AND newer.person_id = review.person_id
                      AND (
                        newer.created_at > review.created_at OR
                        (newer.created_at = review.created_at
                         AND newer.review_id > review.review_id)
                      )
                  )) AS rejected_prototype_count,
              (SELECT COUNT(*) FROM voice_prototypes candidate
                WHERE candidate.status = 'candidate'
                  AND candidate.quality_score >= COALESCE(
                    policy.minimum_quality, 0.50
                  )
                  AND COALESCE(
                    (SELECT decision.candidate_person_id
                     FROM speaker_match_decisions decision
                     WHERE decision.prototype_id = candidate.prototype_id
                     ORDER BY decision.created_at DESC, decision.decision_id DESC
                     LIMIT 1),
                    (SELECT link.person_id FROM person_cluster_links link
                     WHERE link.cluster_id = candidate.cluster_id
                       AND link.status = 'active'),
                    (SELECT cluster.suggested_person_id FROM speaker_clusters cluster
                     WHERE cluster.cluster_id = candidate.cluster_id)
                  ) = p.person_id
                  AND NOT EXISTS (
                    SELECT 1 FROM voice_prototype_reviews review
                    WHERE review.prototype_id = candidate.prototype_id
                      AND review.person_id = p.person_id
                      AND review.decision IN ('confirmed', 'rejected', 'retracted')
                      AND NOT EXISTS (
                        SELECT 1 FROM voice_prototype_reviews newer
                        WHERE newer.prototype_id = review.prototype_id
                          AND newer.person_id = review.person_id
                          AND (
                            newer.created_at > review.created_at OR
                            (newer.created_at = review.created_at
                             AND newer.review_id > review.review_id)
                          )
                      )
                  )) AS pending_voice_review_count
            FROM persons p
            LEFT JOIN person_identity_policy_revisions policy
              ON policy.person_id = p.person_id AND NOT EXISTS (
                SELECT 1 FROM person_identity_policy_revisions newer_policy
                WHERE newer_policy.person_id = policy.person_id
                  AND newer_policy.revision > policy.revision
              )
            ORDER BY p.kind, lower(p.display_name), p.person_id
            """
        ).fetchall()
        return tuple(
            {
                **_row(row),
                "known_auto_match_enabled": bool(row["known_auto_match_enabled"] or 0),
                "voice_calibration": json.loads(row["calibration_json"] or "{}"),
            }
            for row in rows
        )
    def list_clusters(self, status: str | None, limit: int) -> tuple[dict[str, Any], ...]:
        where = "WHERE c.status = ?" if status else ""
        parameters: tuple[object, ...] = (status, limit) if status else (limit,)
        rows = self.connection.execute(
            f"""
            SELECT c.*, l.person_id, p.display_name AS person_name,
              suggested.display_name AS suggested_person_name,
              l.source AS link_source, l.confidence AS link_confidence,
              COUNT(DISTINCT m.speaker_track_id) AS track_count,
              COUNT(DISTINCT t.session_id) AS session_count,
              MAX(t.session_id) AS latest_session_id,
              GROUP_CONCAT(DISTINCT t.session_id) AS session_ids_csv
            FROM speaker_clusters c
            LEFT JOIN person_cluster_links l
              ON l.cluster_id = c.cluster_id AND l.status = 'active'
            LEFT JOIN persons p ON p.person_id = l.person_id
            LEFT JOIN persons suggested
              ON suggested.person_id = c.suggested_person_id
            LEFT JOIN speaker_cluster_memberships m
              ON m.cluster_id = c.cluster_id AND m.state = 'active'
            LEFT JOIN speaker_tracks t ON t.speaker_track_id = m.speaker_track_id
            {where}
            GROUP BY c.cluster_id
            ORDER BY c.updated_at DESC, c.cluster_id DESC LIMIT ?
            """,
            parameters,
        ).fetchall()
        return tuple(_cluster(row) for row in rows)
    def cluster_detail(self, cluster_id: str) -> dict[str, Any]:
        rows = self.connection.execute(
            """
            SELECT c.*, l.person_id, p.display_name AS person_name,
              suggested.display_name AS suggested_person_name,
              l.source AS link_source, l.confidence AS link_confidence,
              COUNT(DISTINCT m.speaker_track_id) AS track_count,
              COUNT(DISTINCT t.session_id) AS session_count,
              MAX(t.session_id) AS latest_session_id,
              GROUP_CONCAT(DISTINCT t.session_id) AS session_ids_csv
            FROM speaker_clusters c
            LEFT JOIN person_cluster_links l
              ON l.cluster_id = c.cluster_id AND l.status = 'active'
            LEFT JOIN persons p ON p.person_id = l.person_id
            LEFT JOIN persons suggested
              ON suggested.person_id = c.suggested_person_id
            LEFT JOIN speaker_cluster_memberships m
              ON m.cluster_id = c.cluster_id AND m.state = 'active'
            LEFT JOIN speaker_tracks t ON t.speaker_track_id = m.speaker_track_id
            WHERE c.cluster_id = ? GROUP BY c.cluster_id
            """,
            (cluster_id,),
        ).fetchone()
        if rows is None:
            raise KeyError(f"speaker cluster does not exist: {cluster_id}")
        members = self.connection.execute(
            """
            SELECT m.membership_id, m.speaker_track_id, m.source, m.confidence,
              t.session_id, t.label, t.created_at
            FROM speaker_cluster_memberships m
            JOIN speaker_tracks t ON t.speaker_track_id = m.speaker_track_id
            WHERE m.cluster_id = ? AND m.state = 'active'
            ORDER BY t.created_at DESC, t.speaker_track_id
            """,
            (cluster_id,),
        ).fetchall()
        prototypes = self.connection.execute(
            """
            SELECT DISTINCT v.prototype_id, v.speaker_track_id, v.status,
              v.quality_score, v.representative_clips_json, v.created_at,
              decision.decision_tier, decision.candidate_person_id,
              decision.best_score, decision.second_best_score,
              decision.score_margin, decision.reason AS match_reason,
              review.person_id AS reviewed_person_id,
              review.decision AS review_status, review.note AS review_note
            FROM voice_prototypes v
            JOIN speaker_cluster_memberships m
              ON m.speaker_track_id = v.speaker_track_id AND m.state = 'active'
            LEFT JOIN speaker_match_decisions decision
              ON decision.prototype_id = v.prototype_id
             AND NOT EXISTS (
               SELECT 1 FROM speaker_match_decisions newer
               WHERE newer.prototype_id = decision.prototype_id
                 AND (
                   newer.created_at > decision.created_at OR
                   (newer.created_at = decision.created_at
                    AND newer.decision_id > decision.decision_id)
                 )
             )
            LEFT JOIN voice_prototype_reviews review
              ON review.prototype_id = v.prototype_id
             AND NOT EXISTS (
               SELECT 1 FROM voice_prototype_reviews newer_review
               WHERE newer_review.prototype_id = review.prototype_id
                 AND (
                   newer_review.created_at > review.created_at OR
                   (newer_review.created_at = review.created_at
                    AND newer_review.review_id > review.review_id)
                 )
             )
            WHERE m.cluster_id = ?
            ORDER BY v.quality_score DESC, v.created_at, v.prototype_id
            """,
            (cluster_id,),
        ).fetchall()
        operations = self.connection.execute(
            """
            SELECT operation_id, kind, actor, payload_json, reverts_operation_id, created_at
            FROM person_cluster_operations WHERE cluster_id = ?
            ORDER BY created_at DESC, operation_id DESC LIMIT 100
            """,
            (cluster_id,),
        ).fetchall()
        detail = _cluster(rows)
        detail["members"] = [_row(row) for row in members]
        detail["prototypes"] = [
            {
                **_row(row),
                "representative_clips": json.loads(row["representative_clips_json"]),
            }
            for row in prototypes
        ]
        detail["operations"] = [
            {**_row(row), "payload": json.loads(row["payload_json"])}
            for row in operations
        ]
        return detail
    def create_person(
        self, person_id: str, display_name: str, kind: str, created_at: str
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO persons (
              person_id, display_name, kind, user_confirmed, revision, created_at, updated_at
            ) VALUES (?, ?, ?, 1, 1, ?, ?)
            """,
            (person_id, display_name, kind, created_at, created_at),
        )
        self.connection.execute(
            """
            INSERT INTO person_profile_revisions (
              person_id, revision, display_name, aliases_json,
              relationship_labels_json, notes, actor, created_at
            ) VALUES (?, 1, ?, '[]', '[]', '', 'desktop-user', ?)
            """,
            (person_id, display_name, created_at),
        )
        self._create_identity_policy(person_id, created_at, "desktop-user")
    def import_person(
        self,
        person_id: str,
        display_name: str,
        kind: str,
        aliases: tuple[str, ...],
        relationship_labels: tuple[str, ...],
        actor: str,
        created_at: str,
    ) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO persons (
              person_id, display_name, kind, user_confirmed, revision,
              created_at, updated_at
            ) VALUES (?, ?, ?, 1, 1, ?, ?)
            ON CONFLICT(person_id) DO NOTHING
            """,
            (person_id, display_name, kind, created_at, created_at),
        )
        if cursor.rowcount != 1:
            return False
        self.connection.execute(
            """
            INSERT INTO person_profile_revisions (
              person_id, revision, display_name, aliases_json,
              relationship_labels_json, notes, actor, created_at
            ) VALUES (?, 1, ?, ?, ?, '', ?, ?)
            """,
            (
                person_id,
                display_name,
                _json(list(aliases)),
                _json(list(relationship_labels)),
                actor,
                created_at,
            ),
        )
        self._create_identity_policy(person_id, created_at, actor)
        return True
