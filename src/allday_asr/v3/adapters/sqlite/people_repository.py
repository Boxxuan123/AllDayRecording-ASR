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
            SELECT speaker_track_id FROM speaker_tracks
            WHERE session_id = ? AND NOT EXISTS (
              SELECT 1 FROM speaker_cluster_memberships m
              WHERE m.speaker_track_id = speaker_tracks.speaker_track_id
                AND m.state = 'active'
            ) ORDER BY speaker_track_id
            """,
            (session_id,),
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
              AND p.model = ? AND p.model_version = ?
              AND EXISTS (
                SELECT 1 FROM person_cluster_links l
                WHERE l.cluster_id = p.cluster_id AND l.person_id = p.person_id
                  AND l.status = 'active'
              )
            ORDER BY p.created_at, p.prototype_id
            """,
            (model, model_version),
        ).fetchall()
        return _vectors(rows, "person_id")

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
    ) -> None:
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
            ) VALUES (?, ?, ?, 'active', 'automatic', ?, 1, ?, ?, ?)
            """,
            (
                membership_id,
                cluster_id,
                embedding.speaker_track_id,
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
            ) VALUES (?, 'analyze', ?, 'system:local-clustering', ?, ?)
            """,
            (
                operation_id,
                cluster_id,
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
                WHERE v.person_id = p.person_id AND v.status = 'accepted') AS prototype_count
            FROM persons p ORDER BY p.kind, lower(p.display_name), p.person_id
            """
        ).fetchall()
        return tuple(_row(row) for row in rows)

    def list_clusters(self, status: str | None, limit: int) -> tuple[dict[str, Any], ...]:
        where = "WHERE c.status = ?" if status else ""
        parameters: tuple[object, ...] = (status, limit) if status else (limit,)
        rows = self.connection.execute(
            f"""
            SELECT c.*, l.person_id, p.display_name AS person_name,
              COUNT(DISTINCT m.speaker_track_id) AS track_count,
              COUNT(DISTINCT t.session_id) AS session_count,
              MAX(t.session_id) AS latest_session_id
            FROM speaker_clusters c
            LEFT JOIN person_cluster_links l
              ON l.cluster_id = c.cluster_id AND l.status = 'active'
            LEFT JOIN persons p ON p.person_id = l.person_id
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
              COUNT(DISTINCT m.speaker_track_id) AS track_count,
              COUNT(DISTINCT t.session_id) AS session_count,
              MAX(t.session_id) AS latest_session_id
            FROM speaker_clusters c
            LEFT JOIN person_cluster_links l
              ON l.cluster_id = c.cluster_id AND l.status = 'active'
            LEFT JOIN persons p ON p.person_id = l.person_id
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
            SELECT DISTINCT v.prototype_id, v.speaker_track_id, v.status, v.quality_score,
              v.representative_clips_json, v.created_at
            FROM voice_prototypes v
            JOIN speaker_cluster_memberships m
              ON m.speaker_track_id = v.speaker_track_id AND m.state = 'active'
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

    def label_cluster(
        self, cluster_id: str, person_id: str, actor: str, operation_id: str, created_at: str
    ) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
        cluster = self._active_cluster(cluster_id)
        person = self.connection.execute(
            "SELECT person_id FROM persons WHERE person_id = ? AND kind != 'unknown'",
            (person_id,),
        ).fetchone()
        if person is None:
            raise KeyError(f"person does not exist: {person_id}")
        previous = self.connection.execute(
            """
            SELECT link_id, person_id FROM person_cluster_links
            WHERE cluster_id = ? AND status = 'active'
            """,
            (cluster_id,),
        ).fetchone()
        previous_person = str(previous["person_id"]) if previous is not None else None
        if previous is not None and previous_person == person_id:
            raise ValueError("speaker cluster is already linked to this person")
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
            ) VALUES (?, ?, ?, 'active', 1, 'human', 1, ?, ?, ?)
            """,
            (link_id, cluster_id, person_id, operation_id, created_at, created_at),
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
        for index, candidate in enumerate(candidates):
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
                "person_id": person_id,
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
                self.connection.execute(
                    """
                    INSERT INTO person_cluster_links (
                      link_id, cluster_id, person_id, status, confidence, source,
                      revision, operation_id, created_at, updated_at
                    ) VALUES (?, ?, ?, 'active', 1, 'human', 1, ?, ?, ?)
                    """,
                    (
                        f"link-{undo_operation_id}",
                        cluster_id,
                        previous_person,
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
            ORDER BY u.start_at, u.utterance_id
            """,
            (cluster_id, session_id),
        ).fetchall()
        return tuple(str(row["utterance_id"]) for row in rows)

    def _cluster_utterance_ids(self, cluster_id: str) -> tuple[str, ...]:
        rows = self.connection.execute(
            """
            SELECT u.utterance_id FROM speaker_cluster_memberships m
            JOIN utterances u ON u.speaker_track_id = m.speaker_track_id
            WHERE m.cluster_id = ? AND m.state = 'active' AND u.status = 'active'
            ORDER BY u.start_at, u.utterance_id
            """,
            (cluster_id,),
        ).fetchall()
        return tuple(str(row["utterance_id"]) for row in rows)

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
    confidence = row["suggestion_confidence"]
    value["suggestion_confidence"] = float(confidence) if confidence is not None else None
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
