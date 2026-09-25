from __future__ import annotations
from .people_sample_eligibility import usable_voice_sample

import json
import sqlite3
from collections.abc import Sequence
from typing import Any


from .people_repository_codec import _json, _row


class PeoplePrototypeRepositoryMixin:
    def prototype_retraction_target(self, prototype_id: str, person_id: str) -> dict:
        # Historical authorization is manageable even after its source or review
        # carrier becomes inactive. This lookup must never authorize acceptance.
        row = self.connection.execute("""SELECT p.*, p.cluster_id AS active_cluster_id,
            ? AS linked_person_id FROM voice_prototypes p
            WHERE p.prototype_id=? AND p.status='candidate' AND EXISTS (
              SELECT 1 FROM voice_prototypes a WHERE a.source_prototype_id=p.prototype_id
              AND a.person_id=? AND a.status='accepted' AND a.human_confirmed=1)""",
            (person_id, prototype_id, person_id)).fetchone()
        review = self.latest_prototype_review(prototype_id, person_id)
        if row is None or review is None or review['decision'] != 'confirmed':
            raise ValueError('only a confirmed prototype can be retracted')
        return dict(row)

    def annotation_samples(self, utterance_id: str) -> tuple[dict, ...]:
        rows = self.connection.execute(f"""SELECT p.prototype_id, p.person_id, p.status, p.human_confirmed,
            p.model, p.model_version, p.quality_score, p.representative_clips_json,
            {usable_voice_sample('p')} AS source_usable,
            EXISTS (SELECT 1 FROM person_cluster_links l WHERE l.cluster_id = p.cluster_id
                    AND l.person_id = p.person_id AND l.status = 'active') AS current_link,
            (SELECT decision FROM voice_prototype_reviews r
             WHERE r.prototype_id = COALESCE(p.source_prototype_id, p.prototype_id)
             AND (p.person_id IS NULL OR r.person_id = p.person_id)
             ORDER BY r.created_at DESC, r.review_id DESC LIMIT 1) AS review_decision
            FROM voice_prototypes p WHERE EXISTS (
              SELECT 1 FROM json_each(p.representative_clips_json) c
              WHERE json_extract(c.value, '$.utterance_id') = ? OR EXISTS (
                SELECT 1 FROM utterances u JOIN capture_segments seg ON seg.session_id=u.session_id
                JOIN audio_assets asset ON asset.asset_id=seg.asset_id
                WHERE u.utterance_id=? AND asset.media_id=json_extract(c.value,'$.media_id')
                AND u.start_ms<seg.session_end_ms AND u.end_ms>seg.session_start_ms
                AND json_extract(c.value,'$.start_ms') < seg.source_start_ms+MIN(u.end_ms,seg.session_end_ms)-seg.session_start_ms
                AND json_extract(c.value,'$.end_ms') > seg.source_start_ms+MAX(u.start_ms,seg.session_start_ms)-seg.session_start_ms))
            ORDER BY p.created_at, p.prototype_id""", (utterance_id, utterance_id)).fetchall()
        return tuple(dict(row) for row in rows)

    def prototype_candidate(self, prototype_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            f"""
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
            WHERE candidate.prototype_id = ? AND candidate.status = 'candidate' AND {usable_voice_sample("candidate")}
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
        candidate = (self.prototype_retraction_target(prototype_id, person_id)
                     if decision == 'retracted' else self.prototype_candidate(prototype_id))
        person = self.connection.execute(
            "SELECT kind FROM persons WHERE person_id = ?",
            (person_id,),
        ).fetchone()
        if person is None:
            raise KeyError(f"person does not exist: {person_id}")
        if str(person["kind"]) != "known":
            raise ValueError("known-person prototype review cannot target self")
        if decision == 'confirmed':
            if candidate.get('linked_person_id') != person_id:
                raise ValueError('prototype cluster is not linked to this person')
            if float(candidate['quality_score']) < float(self.identity_policy(person_id)['minimum_quality']):
                raise ValueError('sample does not meet current quality policy')
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
            f"""
            SELECT accepted.prototype_id,
              COALESCE(accepted.source_prototype_id, accepted.prototype_id)
                AS source_prototype_id,
              accepted.model, accepted.model_version, accepted.vector_json,
              accepted.quality_score, track.session_id
            FROM voice_prototypes accepted
            JOIN speaker_tracks track
              ON track.speaker_track_id = accepted.speaker_track_id
            WHERE accepted.person_id = ? AND accepted.status = 'accepted' AND {usable_voice_sample("accepted")}
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
            f"""
            SELECT candidate.prototype_id AS source_prototype_id,
              candidate.model, candidate.model_version, candidate.vector_json,
              candidate.quality_score, track.session_id
            FROM voice_prototype_reviews review
            JOIN voice_prototypes candidate
              ON candidate.prototype_id = review.prototype_id
            JOIN speaker_tracks track
              ON track.speaker_track_id = candidate.speaker_track_id
            WHERE review.person_id = ? AND review.decision = 'rejected'
              AND {usable_voice_sample("candidate")}
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
            f"""
            SELECT candidate.prototype_id, candidate.speaker_track_id,
              COALESCE(membership.cluster_id,candidate.cluster_id) AS cluster_id, track.session_id, candidate.quality_score,
              (track.label LIKE 'manual:%') AS human_selection,
              candidate.representative_clips_json, candidate.created_at,
              COALESCE((SELECT r.person_id FROM voice_prototype_reviews r WHERE r.prototype_id=candidate.prototype_id ORDER BY r.created_at DESC,r.review_id DESC LIMIT 1),link.person_id) AS linked_person_id,
              cluster.suggested_person_id,
              decision.decision_tier, decision.candidate_person_id,
              decision.best_score, decision.second_best_score,
              decision.score_margin, decision.reason AS match_reason
            FROM voice_prototypes candidate
            LEFT JOIN speaker_cluster_memberships membership
              ON membership.speaker_track_id = candidate.speaker_track_id
             AND membership.state = 'active'
            LEFT JOIN speaker_clusters cluster
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
            WHERE candidate.status = 'candidate' AND ({usable_voice_sample("candidate")} OR EXISTS (
                SELECT 1 FROM voice_prototype_reviews r WHERE r.prototype_id=candidate.prototype_id
                AND r.decision='confirmed' AND NOT EXISTS (SELECT 1 FROM voice_prototype_reviews newer
                WHERE newer.prototype_id=r.prototype_id AND newer.person_id=r.person_id
                AND (newer.created_at>r.created_at OR (newer.created_at=r.created_at AND newer.review_id>r.review_id)))))
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
            if review_status != 'confirmed' and (policy is None or float(row['quality_score']) < float(policy['minimum_quality'])):
                continue
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
