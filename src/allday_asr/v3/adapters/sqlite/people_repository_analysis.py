from __future__ import annotations
from .people_sample_eligibility import usable_voice_sample
from .sound_eligibility import usable_sample

import json
from typing import Any

from allday_asr.v3.ports.speaker_embeddings import SpeakerClipInput, SpeakerTrackInput
from allday_asr.v3.domain.sound_kind import sound_uses

from .people_repository_codec import _json, _row, _vectors


class PeopleAnalysisRepositoryMixin:
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
            WHERE t.session_id = ? AND t.label NOT LIKE 'manual:%' AND t.label NOT LIKE 'sample:%'
              AND NOT EXISTS (
                SELECT 1 FROM speaker_cluster_memberships m
                WHERE m.speaker_track_id = t.speaker_track_id AND m.state = 'active'
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
                f"""
                SELECT utterance_id, start_ms, end_ms, evidence_json
                FROM utterances u
                WHERE session_id = ? AND speaker_track_id = ? AND status = 'active'
                  AND {usable_sample("u")}
                ORDER BY (end_ms - start_ms) DESC, start_ms, utterance_id
                LIMIT 12
                """,
                (session_id, track["speaker_track_id"]),
            ).fetchall()
            clips: list[SpeakerClipInput] = []
            for utterance in utterances:
                if not sound_uses(json.loads(utterance["evidence_json"]))["sample_candidate_allowed"]:
                    continue
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
                    if any(c.media_id == str(segment["media_id"]) and
                           c.source_start_ms < source_start + clip_end - overlap_start and
                           c.source_end_ms > source_start for c in clips):
                        continue
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

    def manual_track_cluster(self, track_id: str) -> str | None:
        row = self.connection.execute("""SELECT m.cluster_id FROM speaker_tracks t
            JOIN speaker_cluster_memberships m ON m.speaker_track_id = t.speaker_track_id
              AND m.state = 'active'
            WHERE t.speaker_track_id = ? AND t.label LIKE 'manual:%'""", (track_id,)).fetchone()
        return str(row[0]) if row else None
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
            excluded = self.connection.execute(
                f"""SELECT 1 FROM utterances u WHERE session_id = ? AND status = 'active'
                AND start_ms < ? AND end_ms > ?
                AND NOT {usable_sample("u")} LIMIT 1""",
                (session_id, end_ms, start_ms),
            ).fetchone()
            if excluded is not None:
                raise ValueError("声纹样本范围包含已排除的声音片段")
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
            f"""
            SELECT m.cluster_id, p.vector_json FROM voice_prototypes p
            JOIN speaker_cluster_memberships m
              ON m.speaker_track_id = p.speaker_track_id AND m.state = 'active'
            JOIN speaker_clusters c ON c.cluster_id = m.cluster_id
            LEFT JOIN person_cluster_links l
              ON l.cluster_id = m.cluster_id AND l.status = 'active'
            WHERE p.status = 'candidate' AND {usable_voice_sample("p")} AND c.status = 'active'
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
            f"""
            SELECT p.person_id, p.vector_json FROM voice_prototypes p
            JOIN persons person ON person.person_id = p.person_id
            WHERE p.status = 'accepted' AND {usable_voice_sample("p")} AND p.human_confirmed = 1
              AND person.kind = 'known'
              AND p.quality_score >= COALESCE((SELECT minimum_quality
                FROM person_identity_policy_revisions policy WHERE policy.person_id=p.person_id
                ORDER BY policy.revision DESC LIMIT 1), 0.5)
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
            WHERE c.status = 'active' AND l.link_id IS NULL AND {usable_voice_sample("v")}
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
