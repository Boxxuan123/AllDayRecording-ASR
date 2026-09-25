from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any


from .people_repository_codec import _json


class PeopleClusterRepositoryMixin:
    def add_manual_group(self, cluster_id, track_ids, label, actor, operation_id, created_at):
        self.connection.execute(
            "INSERT INTO speaker_clusters (cluster_id, display_label, status, revision, created_at, updated_at) VALUES (?, ?, 'active', 1, ?, ?)",
            (cluster_id, label, created_at, created_at),
        )
        for index, track_id in enumerate(track_ids):
            self.connection.execute(
                "INSERT INTO speaker_cluster_memberships (membership_id, cluster_id, speaker_track_id, state, source, confidence, revision, operation_id, created_at, updated_at) VALUES (?, ?, ?, 'active', 'human', 1, 1, ?, ?, ?)",
                (f"{operation_id}-{index}", cluster_id, track_id, operation_id, created_at, created_at),
            )
        self._add_operation(operation_id, "label", cluster_id, actor,
                            {"manual_selection": True, "track_ids": list(track_ids)}, created_at)

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
