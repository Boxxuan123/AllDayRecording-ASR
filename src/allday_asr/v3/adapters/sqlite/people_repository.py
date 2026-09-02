from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Any

from allday_asr.v3.domain.people import SpeakerEmbedding
from allday_asr.v3.ports.speaker_embeddings import SpeakerClipInput, SpeakerTrackInput


class SqlitePeopleRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def analysis_inputs(self, session_id: str) -> tuple[SpeakerTrackInput, ...]:
        exists = self.connection.execute(
            "SELECT 1 FROM recording_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if exists is None:
            raise KeyError(f"recording session does not exist: {session_id}")
        tracks = self.connection.execute(
            """
            WITH canonical_run AS (
              SELECT p.run_id FROM processing_runs p
              LEFT JOIN processing_jobs j ON j.run_id = p.run_id
              WHERE p.session_id = ? AND p.status = 'succeeded'
                AND COALESCE(
                  json_extract(j.request_json, '$.admission_mode'),
                  'production'
                ) = 'production'
                AND EXISTS (
                  SELECT 1 FROM utterances u
                  WHERE u.run_id = p.run_id AND u.status = 'active'
                )
              ORDER BY p.created_at DESC, p.run_id DESC
              LIMIT 1
            )
            SELECT t.speaker_track_id FROM speaker_tracks t
            JOIN canonical_run c ON c.run_id = t.run_id
            WHERE t.session_id = ? AND NOT EXISTS (
              SELECT 1 FROM speaker_cluster_memberships m
              WHERE m.speaker_track_id = t.speaker_track_id
                AND m.state = 'active'
            ) ORDER BY t.speaker_track_id
            """,
            (session_id, session_id),
        ).fetchall()
        segments = self.connection.execute(
            """
            SELECT seg.session_start_ms, seg.session_end_ms,
              seg.source_start_ms, a.media_id, r.storage_key
            FROM capture_segments seg
            JOIN audio_assets a ON a.asset_id = seg.asset_id
            JOIN audio_replicas r ON r.replica_id = seg.replica_id
            WHERE seg.session_id = ? AND r.state = 'available'
            ORDER BY seg.sequence, seg.segment_id
            """,
            (session_id,),
        ).fetchall()
        result: list[SpeakerTrackInput] = []
        for track in tracks:
            utterances = self.connection.execute(
                """
                SELECT utterance_id, start_ms, end_ms
                FROM utterances
                WHERE session_id = ? AND speaker_track_id = ? AND status = 'active'
                ORDER BY (end_ms - start_ms) DESC, start_ms, utterance_id
                LIMIT 12
                """,
                (session_id, track["speaker_track_id"]),
            ).fetchall()
            clips: list[SpeakerClipInput] = []
            for utterance in utterances:
                start_ms = int(utterance["start_ms"])
                end_ms = int(utterance["end_ms"])
                for segment in segments:
                    overlap_start = max(start_ms, int(segment["session_start_ms"]))
                    overlap_end = min(end_ms, int(segment["session_end_ms"]))
                    if overlap_end - overlap_start < 800:
                        continue
                    clip_end = min(overlap_end, overlap_start + 8_000)
                    source_start = int(segment["source_start_ms"]) + overlap_start - int(
                        segment["session_start_ms"]
                    )
                    clips.append(
                        SpeakerClipInput(
                            media_id=str(segment["media_id"]),
                            storage_key=str(segment["storage_key"]),
                            source_start_ms=source_start,
                            source_end_ms=source_start + clip_end - overlap_start,
                            utterance_id=str(utterance["utterance_id"]),
                        )
                    )
                    break
                if len(clips) >= 5:
                    break
            if clips:
                result.append(
                    SpeakerTrackInput(
                        speaker_track_id=str(track["speaker_track_id"]),
                        session_id=session_id,
                        clips=tuple(clips),
                    )
                )
        return tuple(result)

    def confirmed_enrollment_input(
        self,
        session_id: str,
        speaker_track_id: str,
        windows: tuple[tuple[int, int], ...],
    ) -> dict[str, Any]:
        if not windows:
            raise ValueError("confirmed enrollment requires at least one window")
        context = self.connection.execute(
            """
            SELECT p.run_id, u.source_artifact_id
            FROM processing_runs p
            JOIN utterances u ON u.run_id = p.run_id AND u.status = 'active'
            WHERE p.session_id = ? AND p.status = 'succeeded'
            ORDER BY p.created_at DESC, p.run_id DESC, u.ordinal, u.utterance_id
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        if context is None:
            raise ValueError("confirmed enrollment session has no succeeded V3 evidence run")
        segments = self.connection.execute(
            """
            SELECT seg.session_start_ms, seg.session_end_ms,
              seg.source_start_ms, a.media_id, r.storage_key
            FROM capture_segments seg
            JOIN audio_assets a ON a.asset_id = seg.asset_id
            JOIN audio_replicas r ON r.replica_id = seg.replica_id
            WHERE seg.session_id = ? AND r.state = 'available'
            ORDER BY seg.sequence, seg.segment_id
            """,
            (session_id,),
        ).fetchall()
        clips: list[SpeakerClipInput] = []
        for start_ms, end_ms in windows:
            if start_ms < 0 or end_ms <= start_ms:
                raise ValueError("confirmed enrollment window is invalid")
            for segment in segments:
                overlap_start = max(start_ms, int(segment["session_start_ms"]))
                overlap_end = min(end_ms, int(segment["session_end_ms"]))
                if overlap_end - overlap_start < 800:
                    continue
                clip_end = min(overlap_end, overlap_start + 8_000)
                source_start = int(segment["source_start_ms"]) + overlap_start - int(
                    segment["session_start_ms"]
                )
                clips.append(
                    SpeakerClipInput(
                        media_id=str(segment["media_id"]),
                        storage_key=str(segment["storage_key"]),
                        source_start_ms=source_start,
                        source_end_ms=source_start + clip_end - overlap_start,
                        utterance_id=None,
                    )
                )
        if not clips:
            raise ValueError("confirmed enrollment windows have no available V3 audio")
        return {
            "run_id": str(context["run_id"]),
            "source_artifact_id": str(context["source_artifact_id"]),
            "track": SpeakerTrackInput(
                speaker_track_id=speaker_track_id,
                session_id=session_id,
                clips=tuple(clips),
            ),
        }

    def person_kind(self, person_id: str) -> str:
        row = self.connection.execute(
            "SELECT kind FROM persons WHERE person_id = ? AND kind != 'unknown'",
            (person_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"person does not exist: {person_id}")
        return str(row["kind"])

    def self_person_id(self) -> str | None:
        row = self.connection.execute(
            "SELECT person_id FROM persons WHERE kind = 'self'"
        ).fetchone()
        return str(row["person_id"]) if row is not None else None

    def start_run(
        self,
        run_id: str,
        session_id: str,
        model: str,
        model_version: str,
        policy: dict[str, Any],
        track_count: int,
        created_at: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO speaker_cluster_runs (
              cluster_run_id, session_id, producer, model, model_version,
              policy_json, status, track_count, created_at
            ) VALUES (?, ?, 'local-speaker-clustering', ?, ?, ?, 'running', ?, ?)
            """,
            (
                run_id,
                session_id,
                model,
                model_version,
                _json(policy),
                track_count,
                created_at,
            ),
        )

    def finish_run(self, run_id: str, status: str, completed_at: str, error: str | None) -> None:
        self.connection.execute(
            """
            UPDATE speaker_cluster_runs SET status = ?, completed_at = ?, error = ?
            WHERE cluster_run_id = ? AND status = 'running'
            """,
            (status, completed_at, error, run_id),
        )

    def cluster_vectors(
        self, model: str, model_version: str
    ) -> tuple[tuple[str, tuple[float, ...]], ...]:
        rows = self.connection.execute(
            """
            SELECT m.cluster_id, p.vector_json FROM voice_prototypes p
            JOIN speaker_cluster_memberships m
              ON m.speaker_track_id = p.speaker_track_id AND m.state = 'active'
            JOIN speaker_clusters c ON c.cluster_id = m.cluster_id
            LEFT JOIN person_cluster_links l
              ON l.cluster_id = m.cluster_id AND l.status = 'active'
            WHERE p.status = 'candidate' AND c.status = 'active'
              AND l.link_id IS NULL AND p.model = ? AND p.model_version = ?
            ORDER BY p.created_at, p.prototype_id
            """,
            (model, model_version),
        ).fetchall()
        return _vectors(rows, "cluster_id")

    def person_vectors(
        self, model: str, model_version: str
    ) -> tuple[tuple[str, tuple[float, ...]], ...]:
        rows = self.connection.execute(
            """
            SELECT p.person_id, p.vector_json FROM voice_prototypes p
            JOIN persons person ON person.person_id = p.person_id
            WHERE p.status = 'accepted' AND p.human_confirmed = 1
              AND person.kind = 'known'
              AND p.model = ? AND p.model_version = ?
              AND EXISTS (
                SELECT 1 FROM person_cluster_links l
                WHERE l.cluster_id = p.cluster_id AND l.person_id = p.person_id
                  AND l.status = 'active'
              )
              AND COALESCE(
                (SELECT review.decision FROM voice_prototype_reviews review
                 WHERE review.prototype_id = COALESCE(
                     p.source_prototype_id, p.prototype_id
                   )
                   AND review.person_id = p.person_id
                 ORDER BY review.created_at DESC, review.review_id DESC
                 LIMIT 1),
                'confirmed'
              ) = 'confirmed'
            ORDER BY p.created_at, p.prototype_id
            """,
            (model, model_version),
        ).fetchall()
        return _vectors(rows, "person_id")

    def identity_policies(self) -> dict[str, dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT policy.* FROM person_identity_policy_revisions policy
            JOIN persons person ON person.person_id = policy.person_id
            WHERE person.kind = 'known' AND NOT EXISTS (
              SELECT 1 FROM person_identity_policy_revisions newer
              WHERE newer.person_id = policy.person_id
                AND newer.revision > policy.revision
            )
            ORDER BY policy.person_id
            """
        ).fetchall()
        return {
            str(row["person_id"]): {
                **_row(row),
                "auto_match_enabled": bool(row["auto_match_enabled"]),
                "calibration": json.loads(row["calibration_json"]),
            }
            for row in rows
        }

    def identity_policy(self, person_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT policy.* FROM person_identity_policy_revisions policy
            WHERE policy.person_id = ?
            ORDER BY policy.revision DESC LIMIT 1
            """,
            (person_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"person identity policy does not exist: {person_id}")
        return {
            **_row(row),
            "auto_match_enabled": bool(row["auto_match_enabled"]),
            "calibration": json.loads(row["calibration_json"]),
        }

    def add_identity_policy_revision(
        self,
        person_id: str,
        *,
        maturity_status: str,
        auto_match_enabled: bool,
        suggest_threshold: float,
        auto_accept_threshold: float,
        minimum_margin: float,
        minimum_quality: float,
        calibration: dict[str, Any],
        actor: str,
        created_at: str,
    ) -> dict[str, Any]:
        current = self.identity_policy(person_id)
        revision = int(current["revision"]) + 1
        self.connection.execute(
            """
            INSERT INTO person_identity_policy_revisions (
              person_id, revision, maturity_status, auto_match_enabled,
              suggest_threshold, auto_accept_threshold, minimum_margin,
              minimum_quality, calibration_json, actor, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                person_id,
                revision,
                maturity_status,
                int(auto_match_enabled),
                suggest_threshold,
                auto_accept_threshold,
                minimum_margin,
                minimum_quality,
                _json(calibration),
                actor,
                created_at,
            ),
        )
        return self.identity_policy(person_id)

    def unlinked_cluster_embeddings(
        self, session_id: str | None = None
    ) -> tuple[dict[str, Any], ...]:
        session_filter = ""
        parameters: tuple[object, ...] = ()
        if session_id is not None:
            session_filter = """
              AND EXISTS (
                SELECT 1 FROM speaker_cluster_memberships selected_membership
                JOIN speaker_tracks selected_track
                  ON selected_track.speaker_track_id = selected_membership.speaker_track_id
                WHERE selected_membership.cluster_id = c.cluster_id
                  AND selected_membership.state = 'active'
                  AND selected_track.session_id = ?
              )
            """
            parameters = (session_id,)
        rows = self.connection.execute(
            f"""
            SELECT c.cluster_id, c.suggested_person_id,
              v.prototype_id, v.speaker_track_id, v.model,
              v.model_version, v.vector_json, v.quality_score
            FROM speaker_clusters c
            JOIN speaker_cluster_memberships m
              ON m.cluster_id = c.cluster_id AND m.state = 'active'
            JOIN voice_prototypes v
              ON v.speaker_track_id = m.speaker_track_id AND v.status = 'candidate'
            LEFT JOIN person_cluster_links l
              ON l.cluster_id = c.cluster_id AND l.status = 'active'
            WHERE c.status = 'active' AND l.link_id IS NULL
              {session_filter}
            ORDER BY c.cluster_id, v.created_at, v.prototype_id
            """,
            parameters,
        ).fetchall()
        return tuple(
            {
                **_row(row),
                "vector": tuple(
                    float(value) for value in json.loads(row["vector_json"])
                ),
                "quality_score": float(row["quality_score"]),
            }
            for row in rows
        )

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

    def label_cluster(
        self,
        cluster_id: str,
        person_id: str,
        actor: str,
        operation_id: str,
        created_at: str,
        *,
        source: str = "human",
        confidence: float = 1.0,
        promote_candidates: bool = False,
    ) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
        if source not in {"human", "automatic"}:
            raise ValueError("speaker cluster link source is invalid")
        if not 0 <= confidence <= 1:
            raise ValueError("speaker cluster link confidence is invalid")
        cluster = self._active_cluster(cluster_id)
        person = self.connection.execute(
            "SELECT person_id FROM persons WHERE person_id = ? AND kind != 'unknown'",
            (person_id,),
        ).fetchone()
        if person is None:
            raise KeyError(f"person does not exist: {person_id}")
        previous = self.connection.execute(
            """
            SELECT link_id, person_id, source, confidence FROM person_cluster_links
            WHERE cluster_id = ? AND status = 'active'
            """,
            (cluster_id,),
        ).fetchone()
        previous_person = str(previous["person_id"]) if previous is not None else None
        if (
            previous is not None
            and previous_person == person_id
            and (source == "automatic" or str(previous["source"]) == "human")
        ):
            raise ValueError("speaker cluster is already linked to this person")
        previous_source = str(previous["source"]) if previous is not None else None
        previous_confidence = (
            float(previous["confidence"]) if previous is not None else None
        )
        if previous is not None:
            self.connection.execute(
                """
                UPDATE person_cluster_links SET status = 'revoked', revision = revision + 1,
                  updated_at = ? WHERE link_id = ?
                """,
                (created_at, previous["link_id"]),
            )
        if previous_person is None:
            rebound_event_ids = tuple(
                str(value["event_id"]) for value in self.events_referencing(cluster_id)
            )
        else:
            prior_operation = self.connection.execute(
                """
                SELECT payload_json FROM person_cluster_operations
                WHERE cluster_id = ? AND kind = 'label'
                ORDER BY created_at DESC, operation_id DESC LIMIT 1
                """,
                (cluster_id,),
            ).fetchone()
            rebound_event_ids = (
                tuple(json.loads(prior_operation["payload_json"]).get("rebound_event_ids", ()))
                if prior_operation is not None
                else ()
            )
        link_id = f"link-{operation_id}"
        self.connection.execute(
            """
            INSERT INTO person_cluster_links (
              link_id, cluster_id, person_id, status, confidence, source,
              revision, operation_id, created_at, updated_at
            ) VALUES (?, ?, ?, 'active', ?, ?, 1, ?, ?, ?)
            """,
            (
                link_id,
                cluster_id,
                person_id,
                confidence,
                source,
                operation_id,
                created_at,
                created_at,
            ),
        )
        candidates = self.connection.execute(
            """
            SELECT prototype_id, speaker_track_id, model, model_version, dimensions,
              vector_json, representative_clips_json, quality_score
            FROM voice_prototypes v
            WHERE v.status = 'candidate' AND EXISTS (
              SELECT 1 FROM speaker_cluster_memberships m
              WHERE m.speaker_track_id = v.speaker_track_id
                AND m.cluster_id = ? AND m.state = 'active'
            )
            ORDER BY created_at, prototype_id
            """,
            (cluster_id,),
        ).fetchall()
        accepted_ids: list[str] = []
        for index, candidate in enumerate(candidates if promote_candidates else ()):
            accepted_id = f"accepted-{operation_id}-{index}"
            accepted_ids.append(accepted_id)
            self.connection.execute(
                """
                INSERT INTO voice_prototypes (
                  prototype_id, speaker_track_id, cluster_id, person_id, status,
                  model, model_version, dimensions, vector_json,
                  representative_clips_json, quality_score, human_confirmed,
                  source_prototype_id, operation_id, created_at
                ) VALUES (?, ?, ?, ?, 'accepted', ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    accepted_id,
                    candidate["speaker_track_id"],
                    cluster_id,
                    person_id,
                    candidate["model"],
                    candidate["model_version"],
                    candidate["dimensions"],
                    candidate["vector_json"],
                    candidate["representative_clips_json"],
                    candidate["quality_score"],
                    candidate["prototype_id"],
                    operation_id,
                    created_at,
                ),
            )
        self.connection.execute(
            """
            UPDATE speaker_clusters SET suggested_person_id = NULL,
              suggestion_confidence = NULL, revision = revision + 1, updated_at = ?
            WHERE cluster_id = ?
            """,
            (created_at, cluster_id),
        )
        self._add_operation(
            operation_id,
            "label",
            cluster_id,
            actor,
            {
                "previous_person_id": previous_person,
                "previous_source": previous_source,
                "previous_confidence": previous_confidence,
                "person_id": person_id,
                "source": source,
                "confidence": confidence,
                "promoted_candidates": promote_candidates,
                "accepted_prototype_ids": accepted_ids,
                "rebound_event_ids": list(rebound_event_ids),
                "previous_cluster_revision": int(cluster["revision"]),
            },
            created_at,
        )
        evidence_ids = self._cluster_utterance_ids(cluster_id)
        return previous_person, evidence_ids, rebound_event_ids

    def merge_clusters(
        self,
        source_cluster_ids: Sequence[str],
        target_cluster_id: str,
        actor: str,
        operation_id: str,
        created_at: str,
    ) -> None:
        target = self._active_cluster(target_cluster_id)
        sources = tuple(dict.fromkeys(source_cluster_ids))
        if not sources or target_cluster_id in sources:
            raise ValueError("merge requires distinct source and target clusters")
        target_person = self._linked_person(target_cluster_id)
        if target_person is not None:
            raise ValueError("only anonymous clusters can be merged")
        moved: list[dict[str, str]] = []
        for source_id in sources:
            self._active_cluster(source_id)
            source_person = self._linked_person(source_id)
            if source_person is not None:
                raise ValueError("only anonymous clusters can be merged")
            rows = self.connection.execute(
                """
                SELECT membership_id FROM speaker_cluster_memberships
                WHERE cluster_id = ? AND state = 'active'
                """,
                (source_id,),
            ).fetchall()
            for row in rows:
                self.connection.execute(
                    """
                    UPDATE speaker_cluster_memberships
                    SET cluster_id = ?, source = 'human', revision = revision + 1,
                      operation_id = ?, updated_at = ? WHERE membership_id = ?
                    """,
                    (target_cluster_id, operation_id, created_at, row["membership_id"]),
                )
                moved.append(
                    {"membership_id": str(row["membership_id"]), "from_cluster_id": source_id}
                )
            self.connection.execute(
                """
                UPDATE speaker_clusters SET status = 'merged', merged_into_cluster_id = ?,
                  revision = revision + 1, updated_at = ? WHERE cluster_id = ?
                """,
                (target_cluster_id, created_at, source_id),
            )
        self.connection.execute(
            """
            UPDATE speaker_clusters SET revision = revision + 1, updated_at = ?
            WHERE cluster_id = ?
            """,
            (created_at, target_cluster_id),
        )
        self._add_operation(
            operation_id,
            "merge",
            target_cluster_id,
            actor,
            {
                "source_cluster_ids": list(sources),
                "moved_memberships": moved,
                "previous_target_revision": int(target["revision"]),
            },
            created_at,
        )

    def split_cluster(
        self,
        cluster_id: str,
        speaker_track_ids: Sequence[str],
        new_cluster_id: str,
        new_label: str,
        actor: str,
        operation_id: str,
        created_at: str,
    ) -> None:
        self._active_cluster(cluster_id)
        track_ids = tuple(dict.fromkeys(speaker_track_ids))
        if not track_ids:
            raise ValueError("split requires at least one speaker track")
        rows = self.connection.execute(
            f"""
            SELECT membership_id, speaker_track_id FROM speaker_cluster_memberships
            WHERE cluster_id = ? AND state = 'active'
              AND speaker_track_id IN ({','.join('?' for _ in track_ids)})
            """,
            (cluster_id, *track_ids),
        ).fetchall()
        if len(rows) != len(track_ids):
            raise ValueError("split speaker tracks must be active members of the cluster")
        total = self.connection.execute(
            """
            SELECT COUNT(*) FROM speaker_cluster_memberships
            WHERE cluster_id = ? AND state = 'active'
            """,
            (cluster_id,),
        ).fetchone()[0]
        if total == len(rows):
            raise ValueError("split must leave at least one track in the original cluster")
        self.connection.execute(
            """
            INSERT INTO speaker_clusters (
              cluster_id, display_label, status, revision, created_at, updated_at
            ) VALUES (?, ?, 'active', 1, ?, ?)
            """,
            (new_cluster_id, new_label, created_at, created_at),
        )
        for row in rows:
            self.connection.execute(
                """
                UPDATE speaker_cluster_memberships
                SET cluster_id = ?, source = 'human', revision = revision + 1,
                  operation_id = ?, updated_at = ? WHERE membership_id = ?
                """,
                (new_cluster_id, operation_id, created_at, row["membership_id"]),
            )
        self.connection.execute(
            """
            UPDATE speaker_clusters SET revision = revision + 1, updated_at = ?
            WHERE cluster_id = ?
            """,
            (created_at, cluster_id),
        )
        self._add_operation(
            operation_id,
            "split",
            cluster_id,
            actor,
            {
                "new_cluster_id": new_cluster_id,
                "membership_ids": [str(row["membership_id"]) for row in rows],
            },
            created_at,
        )

    def ignore_cluster(
        self, cluster_id: str, reason: str, actor: str, operation_id: str, created_at: str
    ) -> None:
        self._active_cluster(cluster_id)
        if not reason.strip():
            raise ValueError("ignore reason is required")
        self.connection.execute(
            """
            UPDATE speaker_clusters SET status = 'ignored', ignored_reason = ?,
              revision = revision + 1, updated_at = ? WHERE cluster_id = ?
            """,
            (reason.strip(), created_at, cluster_id),
        )
        self._add_operation(
            operation_id, "ignore", cluster_id, actor, {"reason": reason.strip()}, created_at
        )

    def undo(
        self, cluster_id: str, actor: str, undo_operation_id: str, created_at: str
    ) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT o.* FROM person_cluster_operations o
            WHERE o.cluster_id = ? AND o.kind IN ('label', 'merge', 'split', 'ignore')
              AND NOT EXISTS (
                SELECT 1 FROM person_cluster_operations u
                WHERE u.reverts_operation_id = o.operation_id
              )
            ORDER BY o.created_at DESC, o.operation_id DESC LIMIT 1
            """,
            (cluster_id,),
        ).fetchone()
        if row is None:
            raise ValueError("speaker cluster has no reversible operation")
        payload = json.loads(row["payload_json"])
        kind = str(row["kind"])
        if kind == "label":
            current = self.connection.execute(
                """
                SELECT link_id FROM person_cluster_links
                WHERE cluster_id = ? AND status = 'active'
                """,
                (cluster_id,),
            ).fetchone()
            if current is not None:
                self.connection.execute(
                    """
                    UPDATE person_cluster_links SET status = 'revoked', revision = revision + 1,
                      updated_at = ? WHERE link_id = ?
                    """,
                    (created_at, current["link_id"]),
                )
            previous_person = payload.get("previous_person_id")
            if previous_person:
                previous_source = str(payload.get("previous_source") or "human")
                previous_confidence = float(payload.get("previous_confidence") or 1.0)
                self.connection.execute(
                    """
                    INSERT INTO person_cluster_links (
                      link_id, cluster_id, person_id, status, confidence, source,
                      revision, operation_id, created_at, updated_at
                    ) VALUES (?, ?, ?, 'active', ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        f"link-{undo_operation_id}",
                        cluster_id,
                        previous_person,
                        previous_confidence,
                        previous_source,
                        undo_operation_id,
                        created_at,
                        created_at,
                    ),
                )
        elif kind == "merge":
            for moved in payload["moved_memberships"]:
                self.connection.execute(
                    """
                    UPDATE speaker_cluster_memberships SET cluster_id = ?,
                      revision = revision + 1, operation_id = ?, updated_at = ?
                    WHERE membership_id = ?
                    """,
                    (
                        moved["from_cluster_id"],
                        undo_operation_id,
                        created_at,
                        moved["membership_id"],
                    ),
                )
            for source_id in payload["source_cluster_ids"]:
                self.connection.execute(
                    """
                    UPDATE speaker_clusters SET status = 'active', merged_into_cluster_id = NULL,
                      revision = revision + 1, updated_at = ? WHERE cluster_id = ?
                    """,
                    (created_at, source_id),
                )
        elif kind == "split":
            for membership_id in payload["membership_ids"]:
                self.connection.execute(
                    """
                    UPDATE speaker_cluster_memberships SET cluster_id = ?,
                      revision = revision + 1, operation_id = ?, updated_at = ?
                    WHERE membership_id = ?
                    """,
                    (cluster_id, undo_operation_id, created_at, membership_id),
                )
            self.connection.execute(
                """
                UPDATE speaker_clusters SET status = 'split', revision = revision + 1,
                  updated_at = ? WHERE cluster_id = ?
                """,
                (created_at, payload["new_cluster_id"]),
            )
        elif kind == "ignore":
            self.connection.execute(
                """
                UPDATE speaker_clusters SET status = 'active', ignored_reason = NULL,
                  revision = revision + 1, updated_at = ? WHERE cluster_id = ?
                """,
                (created_at, cluster_id),
            )
        self.connection.execute(
            """
            UPDATE speaker_clusters SET revision = revision + 1, updated_at = ?
            WHERE cluster_id = ?
            """,
            (created_at, cluster_id),
        )
        self.connection.execute(
            """
            INSERT INTO person_cluster_operations (
              operation_id, kind, cluster_id, actor, payload_json,
              reverts_operation_id, created_at
            ) VALUES (?, 'undo', ?, ?, ?, ?, ?)
            """,
            (
                undo_operation_id,
                cluster_id,
                actor,
                _json({"reverted_kind": kind}),
                row["operation_id"],
                created_at,
            ),
        )
        return {
            "operation_id": str(row["operation_id"]),
            "kind": kind,
            "payload": payload,
        }

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

    def prototype_candidate(self, prototype_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT candidate.*, membership.cluster_id AS active_cluster_id,
              track.session_id, link.person_id AS linked_person_id,
              cluster.suggested_person_id
            FROM voice_prototypes candidate
            JOIN speaker_cluster_memberships membership
              ON membership.speaker_track_id = candidate.speaker_track_id
             AND membership.state = 'active'
            JOIN speaker_tracks track
              ON track.speaker_track_id = candidate.speaker_track_id
            JOIN speaker_clusters cluster
              ON cluster.cluster_id = membership.cluster_id
             AND cluster.status = 'active'
            LEFT JOIN person_cluster_links link
              ON link.cluster_id = membership.cluster_id AND link.status = 'active'
            WHERE candidate.prototype_id = ? AND candidate.status = 'candidate'
            """,
            (prototype_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"voice prototype candidate does not exist: {prototype_id}")
        return {
            **_row(row),
            "vector": tuple(
                float(value) for value in json.loads(row["vector_json"])
            ),
            "representative_clips": json.loads(row["representative_clips_json"]),
        }

    def add_prototype_review(
        self,
        *,
        review_id: str,
        accepted_prototype_id: str,
        prototype_id: str,
        person_id: str,
        decision: str,
        actor: str,
        note: str,
        created_at: str,
    ) -> dict[str, Any]:
        if decision not in {"confirmed", "rejected", "uncertain", "retracted"}:
            raise ValueError("voice prototype review decision is invalid")
        candidate = self.prototype_candidate(prototype_id)
        person = self.connection.execute(
            "SELECT kind FROM persons WHERE person_id = ?",
            (person_id,),
        ).fetchone()
        if person is None:
            raise KeyError(f"person does not exist: {person_id}")
        if str(person["kind"]) != "known":
            raise ValueError("known-person prototype review cannot target self")
        current = self.connection.execute(
            """
            SELECT decision FROM voice_prototype_reviews
            WHERE prototype_id = ? AND person_id = ?
            ORDER BY created_at DESC, review_id DESC LIMIT 1
            """,
            (prototype_id, person_id),
        ).fetchone()
        current_decision = str(current["decision"]) if current is not None else None
        if decision == "retracted" and current_decision != "confirmed":
            raise ValueError("only a confirmed prototype can be retracted")
        if current_decision == decision:
            raise ValueError("voice prototype already has this review decision")
        self.connection.execute(
            """
            INSERT INTO voice_prototype_reviews (
              review_id, prototype_id, person_id, decision, actor, note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (review_id, prototype_id, person_id, decision, actor, note.strip(), created_at),
        )
        if decision == "confirmed":
            accepted = self.connection.execute(
                """
                SELECT prototype_id FROM voice_prototypes
                WHERE status = 'accepted' AND person_id = ?
                  AND source_prototype_id = ?
                ORDER BY created_at DESC, prototype_id DESC LIMIT 1
                """,
                (person_id, prototype_id),
            ).fetchone()
            if accepted is None:
                self.connection.execute(
                    """
                    INSERT INTO voice_prototypes (
                      prototype_id, speaker_track_id, cluster_id, person_id,
                      status, model, model_version, dimensions, vector_json,
                      representative_clips_json, quality_score, human_confirmed,
                      source_prototype_id, operation_id, created_at
                    ) VALUES (?, ?, ?, ?, 'accepted', ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        accepted_prototype_id,
                        candidate["speaker_track_id"],
                        candidate["active_cluster_id"],
                        person_id,
                        candidate["model"],
                        candidate["model_version"],
                        candidate["dimensions"],
                        _json(list(candidate["vector"])),
                        _json(candidate["representative_clips"]),
                        candidate["quality_score"],
                        prototype_id,
                        review_id,
                        created_at,
                    ),
                )
                accepted_id: str | None = accepted_prototype_id
            else:
                accepted_id = str(accepted["prototype_id"])
        else:
            accepted_id = None
        return {
            "review_id": review_id,
            "prototype_id": prototype_id,
            "person_id": person_id,
            "decision": decision,
            "accepted_prototype_id": accepted_id,
            "created_at": created_at,
        }

    def latest_prototype_review(
        self, prototype_id: str, person_id: str
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT review_id, prototype_id, person_id, decision, actor, note,
              created_at
            FROM voice_prototype_reviews
            WHERE prototype_id = ? AND person_id = ?
            ORDER BY created_at DESC, review_id DESC LIMIT 1
            """,
            (prototype_id, person_id),
        ).fetchone()
        return _row(row) if row is not None else None

    def prototype_review_examples(self, person_id: str) -> dict[str, tuple[dict[str, Any], ...]]:
        positives = self.connection.execute(
            """
            SELECT accepted.prototype_id,
              COALESCE(accepted.source_prototype_id, accepted.prototype_id)
                AS source_prototype_id,
              accepted.model, accepted.model_version, accepted.vector_json,
              accepted.quality_score, track.session_id
            FROM voice_prototypes accepted
            JOIN speaker_tracks track
              ON track.speaker_track_id = accepted.speaker_track_id
            WHERE accepted.person_id = ? AND accepted.status = 'accepted'
              AND accepted.human_confirmed = 1
              AND EXISTS (
                SELECT 1 FROM person_cluster_links link
                WHERE link.cluster_id = accepted.cluster_id
                  AND link.person_id = accepted.person_id
                  AND link.status = 'active'
              )
              AND COALESCE(
                (SELECT review.decision FROM voice_prototype_reviews review
                 WHERE review.prototype_id = COALESCE(
                     accepted.source_prototype_id, accepted.prototype_id
                   )
                   AND review.person_id = accepted.person_id
                 ORDER BY review.created_at DESC, review.review_id DESC
                 LIMIT 1),
                'confirmed'
              ) = 'confirmed'
            ORDER BY accepted.created_at, accepted.prototype_id
            """,
            (person_id,),
        ).fetchall()
        negatives = self.connection.execute(
            """
            SELECT candidate.prototype_id AS source_prototype_id,
              candidate.model, candidate.model_version, candidate.vector_json,
              candidate.quality_score, track.session_id
            FROM voice_prototype_reviews review
            JOIN voice_prototypes candidate
              ON candidate.prototype_id = review.prototype_id
            JOIN speaker_tracks track
              ON track.speaker_track_id = candidate.speaker_track_id
            WHERE review.person_id = ? AND review.decision = 'rejected'
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
            ORDER BY review.created_at, review.review_id
            """,
            (person_id,),
        ).fetchall()

        def examples(rows: Sequence[sqlite3.Row]) -> tuple[dict[str, Any], ...]:
            return tuple(
                {
                    **_row(row),
                    "vector": tuple(
                        float(value) for value in json.loads(row["vector_json"])
                    ),
                    "quality_score": float(row["quality_score"]),
                }
                for row in rows
            )

        return {"positive": examples(positives), "negative": examples(negatives)}

    def list_review_candidates(
        self,
        person_id: str | None,
        status: str | None,
        limit: int,
    ) -> tuple[dict[str, Any], ...]:
        rows = self.connection.execute(
            """
            SELECT candidate.prototype_id, candidate.speaker_track_id,
              membership.cluster_id, track.session_id, candidate.quality_score,
              candidate.representative_clips_json, candidate.created_at,
              link.person_id AS linked_person_id,
              cluster.suggested_person_id,
              decision.decision_tier, decision.candidate_person_id,
              decision.best_score, decision.second_best_score,
              decision.score_margin, decision.reason AS match_reason
            FROM voice_prototypes candidate
            JOIN speaker_cluster_memberships membership
              ON membership.speaker_track_id = candidate.speaker_track_id
             AND membership.state = 'active'
            JOIN speaker_clusters cluster
              ON cluster.cluster_id = membership.cluster_id
             AND cluster.status = 'active'
            JOIN speaker_tracks track
              ON track.speaker_track_id = candidate.speaker_track_id
            LEFT JOIN person_cluster_links link
              ON link.cluster_id = membership.cluster_id AND link.status = 'active'
            LEFT JOIN speaker_match_decisions decision
              ON decision.prototype_id = candidate.prototype_id
             AND NOT EXISTS (
               SELECT 1 FROM speaker_match_decisions newer
               WHERE newer.prototype_id = decision.prototype_id
                 AND (
                   newer.created_at > decision.created_at OR
                   (newer.created_at = decision.created_at
                    AND newer.decision_id > decision.decision_id)
                 )
             )
            WHERE candidate.status = 'candidate'
            ORDER BY candidate.quality_score DESC,
              candidate.created_at DESC, candidate.prototype_id DESC
            """
        ).fetchall()
        policies = self.identity_policies()
        values: list[dict[str, Any]] = []
        for row in rows:
            target_person_id = (
                row["linked_person_id"]
                or row["candidate_person_id"]
                or row["suggested_person_id"]
            )
            if target_person_id is None:
                continue
            target = str(target_person_id)
            if person_id is not None and target != person_id:
                continue
            policy = policies.get(target)
            if policy is None or float(row["quality_score"]) < float(
                policy["minimum_quality"]
            ):
                continue
            review = self.connection.execute(
                """
                SELECT review_id, decision, actor, note, created_at
                FROM voice_prototype_reviews
                WHERE prototype_id = ? AND person_id = ?
                ORDER BY created_at DESC, review_id DESC LIMIT 1
                """,
                (row["prototype_id"], target),
            ).fetchone()
            review_status = str(review["decision"]) if review is not None else "pending"
            if status == "pending" and review_status not in {"pending", "uncertain"}:
                continue
            if status not in {None, "pending"} and review_status != status:
                continue
            person = self.connection.execute(
                "SELECT display_name FROM persons WHERE person_id = ?",
                (target,),
            ).fetchone()
            values.append(
                {
                    **_row(row),
                    "person_id": target,
                    "person_name": str(person["display_name"]),
                    "review_status": review_status,
                    "review": _row(review) if review is not None else None,
                    "representative_clips": json.loads(
                        row["representative_clips_json"]
                    ),
                }
            )
            if len(values) >= limit:
                break
        return tuple(values)

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


def _vectors(
    rows: Sequence[sqlite3.Row], key: str
) -> tuple[tuple[str, tuple[float, ...]], ...]:
    result: list[tuple[str, tuple[float, ...]]] = []
    for row in rows:
        result.append(
            (
                str(row[key]),
                tuple(float(value) for value in json.loads(row["vector_json"])),
            )
        )
    return tuple(result)


def _cluster(row: sqlite3.Row) -> dict[str, Any]:
    value = _row(row)
    value["track_count"] = int(row["track_count"])
    value["session_count"] = int(row["session_count"])
    value["session_ids"] = sorted(
        value
        for value in str(row["session_ids_csv"] or "").split(",")
        if value
    )
    value.pop("session_ids_csv", None)
    confidence = row["suggestion_confidence"]
    value["suggestion_confidence"] = float(confidence) if confidence is not None else None
    link_confidence = row["link_confidence"]
    value["link_confidence"] = (
        float(link_confidence) if link_confidence is not None else None
    )
    return value


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys() if not key.endswith("_json")}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _contains_reference(value: object, reference_id: str) -> bool:
    if value == reference_id:
        return True
    if isinstance(value, dict):
        return any(_contains_reference(item, reference_id) for item in value.values())
    if isinstance(value, list):
        return any(_contains_reference(item, reference_id) for item in value)
    return False


__all__ = ["SqlitePeopleRepository"]
