from __future__ import annotations
import json
from typing import Any

from allday_asr.v3.domain.reviews import (
    ReviewDisposition,
    event_proposal_review_disposition,
    person_memory_review_disposition,
    voice_review_disposition,
)

from .desktop_repository_codec import (
    _job,
)


class DesktopReviewQueryMixin:
    def list_processing_jobs(
        self, status: str | None, limit: int
    ) -> tuple[dict[str, Any], ...]:
        where = "WHERE j.status = ?" if status else ""
        parameters: tuple[object, ...] = (status, limit) if status else (limit,)
        rows = self.connection.execute(
            f"""
            SELECT j.*, p.session_id, p.pipeline_version, p.input_revision,
              p.revision, p.current_stage, p.progress,
              (SELECT COUNT(*) FROM stage_runs s WHERE s.run_id = p.run_id) AS stage_count,
              (SELECT COUNT(*) FROM stage_runs s WHERE s.run_id = p.run_id
                AND s.status = 'succeeded') AS completed_stage_count
            FROM processing_jobs j JOIN processing_runs p ON p.run_id = j.run_id
            {where} ORDER BY j.created_at DESC, j.job_id DESC LIMIT ?
            """,
            parameters,
        ).fetchall()
        return tuple(_job(row) for row in rows)

    def list_reviews(self, limit: int) -> tuple[dict[str, Any], ...]:
        from . import desktop_repository as compatibility

        items: list[dict[str, Any]] = []
        # A processing state is not a human decision. Failures and retryable
        # waiting stages remain visible in the processing center. A future
        # irreversible data-risk gate must be explicitly typed before it can
        # enter this inbox.

        reminders = compatibility.SqliteReminderRepository(
            self.connection
        ).list_candidates("pending_confirmation", limit)
        for candidate in reminders:
            title = str(candidate.get("title") or candidate["operation"])
            evidence = candidate.get("evidence_utterance_ids", ())
            items.append(
                {
                    "review_id": f"reminder:{candidate['candidate_id']}",
                    "kind": "reminder",
                    "priority": "high",
                    "source_id": str(candidate["candidate_id"]),
                    "source_revision": candidate.get("expected_revision"),
                    "session_id": str(candidate["session_id"]),
                    "person_id": str(candidate["actor_person_id"]),
                    "title": title,
                    "summary": title,
                    "reason": "reminder_requires_confirmation",
                    "evidence_count": len(evidence),
                    "created_at": str(candidate["created_at"]),
                    "updated_at": str(candidate["created_at"]),
                    "context": {
                        "operation": str(candidate["operation"]),
                        "scheduled_at": candidate.get("scheduled_at"),
                        "location": candidate.get("location"),
                        "confidence": float(candidate["confidence"]),
                    },
                }
            )

        proposal_rows = self.connection.execute(
            """
            SELECT proposal.proposal_id, proposal.kind, proposal.payload_json,
              proposal.evidence_utterance_ids_json, proposal.created_at,
              generation.layer, generation.producer, generation.model
            FROM structured_change_proposals proposal
            JOIN generation_records generation
              ON generation.generation_id = proposal.generation_id
            WHERE proposal.status = 'pending'
              AND proposal.kind = 'event_operation'
              AND generation.layer = 'event'
              AND NOT EXISTS (
                SELECT 1 FROM reminder_candidates reminder
                WHERE reminder.proposal_id = proposal.proposal_id
              )
            ORDER BY proposal.created_at, proposal.proposal_id LIMIT ?
            """,
            (limit,),
        ).fetchall()
        for row in proposal_rows:
            payload = json.loads(str(row["payload_json"]))
            evidence = json.loads(str(row["evidence_utterance_ids_json"]))
            if (
                event_proposal_review_disposition(
                    payload, evidence_count=len(evidence)
                )
                is not ReviewDisposition.REVIEW_REQUIRED
            ):
                continue
            title, summary, session_id, person_id, confidence = _proposal_copy(
                str(row["kind"]), payload
            )
            items.append(
                {
                    "review_id": f"knowledge_proposal:{row['proposal_id']}",
                    "kind": "knowledge_proposal",
                    "priority": "normal",
                    "source_id": str(row["proposal_id"]),
                    "source_revision": None,
                    "session_id": session_id,
                    "person_id": person_id,
                    "title": title,
                    "summary": summary,
                    "reason": "trusted_memory_requires_confirmation",
                    "evidence_count": len(evidence),
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["created_at"]),
                    "context": {
                        "proposal_kind": str(row["kind"]),
                        "layer": str(row["layer"]),
                        "producer": str(row["producer"]),
                        "model": str(row["model"]),
                        "operation": payload.get("operation"),
                        "event_kind": payload.get("event_kind"),
                        "confidence": confidence,
                        "review_category": "memory",
                    },
                }
            )

        memory_rows = self.connection.execute(
            """
            SELECT current.memory_id, current.revision, current.person_id,
              current.kind, current.summary, current.confidence,
              current.confirmation_status, current.status, current.event_id,
              current.created_at,
              person.display_name,
              COALESCE(
                event.session_id,
                (SELECT utterance.session_id
                 FROM person_memory_evidence evidence
                 JOIN utterances utterance
                   ON utterance.utterance_id = evidence.utterance_id
                 WHERE evidence.memory_id = current.memory_id
                   AND evidence.memory_revision = current.revision
                 ORDER BY evidence.created_at, evidence.link_id LIMIT 1)
              ) AS session_id,
              (SELECT COUNT(*) FROM person_memory_evidence evidence
               WHERE evidence.memory_id = current.memory_id
                 AND evidence.memory_revision = current.revision) AS evidence_count
            FROM person_memory_entries current
            JOIN (
              SELECT memory_id, MAX(revision) AS revision
              FROM person_memory_entries GROUP BY memory_id
            ) latest ON latest.memory_id = current.memory_id
              AND latest.revision = current.revision
            JOIN persons person ON person.person_id = current.person_id
            LEFT JOIN event_current_states event ON event.event_id = current.event_id
            WHERE current.status = 'active'
              AND current.confirmation_status = 'unconfirmed'
            ORDER BY current.created_at, current.memory_id LIMIT ?
            """,
            (limit,),
        ).fetchall()
        for row in memory_rows:
            memory = {
                "kind": str(row["kind"]),
                "confidence": float(row["confidence"]),
                "confirmation_status": str(row["confirmation_status"]),
                "status": str(row["status"]),
                "event_id": row["event_id"],
                "evidence_count": int(row["evidence_count"]),
            }
            if (
                person_memory_review_disposition(memory)
                is not ReviewDisposition.REVIEW_REQUIRED
            ):
                continue
            items.append(
                {
                    "review_id": f"person_memory:{row['memory_id']}:{row['revision']}",
                    "kind": "person_memory",
                    "priority": "normal",
                    "source_id": str(row["memory_id"]),
                    "source_revision": int(row["revision"]),
                    "session_id": (
                        str(row["session_id"])
                        if row["session_id"] is not None
                        else None
                    ),
                    "person_id": str(row["person_id"]),
                    "title": str(row["display_name"]),
                    "summary": str(row["summary"]),
                    "reason": "durable_person_memory_requires_confirmation",
                    "evidence_count": int(row["evidence_count"]),
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["created_at"]),
                    "context": {
                        "memory_kind": str(row["kind"]),
                        "confirmation_status": str(row["confirmation_status"]),
                        "confidence": float(row["confidence"]),
                        "event_id": row["event_id"],
                        "review_category": "memory",
                    },
                }
            )

        voice_candidates = compatibility.SqlitePeopleRepository(
            self.connection
        ).list_review_candidates(None, "pending", limit)
        voice_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for candidate in voice_candidates:
            if (
                voice_review_disposition(candidate)
                is not ReviewDisposition.REVIEW_REQUIRED
            ):
                continue
            lane = "primary"
            key = (
                str(candidate["cluster_id"]),
                str(candidate["person_id"]),
                lane,
            )
            voice_groups.setdefault(key, []).append(candidate)

        reviewed_cluster_ids: set[str] = set()
        for (cluster_id, person_id, lane), candidates in voice_groups.items():
            reviewed_cluster_ids.add(cluster_id)
            prototype_ids = tuple(str(value["prototype_id"]) for value in candidates)
            track_ids = tuple(str(value["speaker_track_id"]) for value in candidates)
            session_ids = tuple(
                dict.fromkeys(str(value["session_id"]) for value in candidates)
            )
            clips = tuple(
                clip
                for value in candidates
                for clip in value.get("representative_clips", ())
            )
            scores = _numbers(value.get("best_score") for value in candidates)
            margins = _numbers(value.get("score_margin") for value in candidates)
            qualities = _numbers(value.get("quality_score") for value in candidates)
            created_at = min(str(value["created_at"]) for value in candidates)
            person_name = str(candidates[0]["person_name"])
            items.append(
                {
                    "review_id": f"voice_identity:{cluster_id}:{person_id}:{lane}",
                    "kind": "voice_identity",
                    "priority": "normal",
                    "source_id": cluster_id,
                    "source_revision": None,
                    "session_id": session_ids[0] if len(session_ids) == 1 else None,
                    "person_id": person_id,
                    "title": person_name,
                    "summary": (
                        f"{len(candidates)} 条声音共同指向 {person_name}"
                        if lane == "primary"
                        else f"{len(candidates)} 条接近阈值的声音可用于改进 {person_name} 的识别"
                    ),
                    "reason": "voice_identity_requires_confirmation",
                    "evidence_count": len(clips),
                    "created_at": created_at,
                    "updated_at": max(str(value["created_at"]) for value in candidates),
                    "context": {
                        "voice_mode": "known_person",
                        "review_lane": lane,
                        "cluster_id": cluster_id,
                        "prototype_ids": list(prototype_ids),
                        "speaker_track_ids": list(track_ids),
                        "session_ids": list(session_ids),
                        "candidate_count": len(candidates),
                        "quality_score": min(qualities) if qualities else None,
                        "decision_tier": candidates[0].get("decision_tier"),
                        "best_score": max(scores) if scores else None,
                        "minimum_score": min(scores) if scores else None,
                        "score_margin": min(margins) if margins else None,
                        "review_status": "pending",
                    },
                }
            )

        clusters = compatibility.SqlitePeopleRepository(self.connection).list_clusters(
            "active", limit
        )
        for cluster in clusters:
            cluster_id = str(cluster["cluster_id"])
            if (
                cluster_id in reviewed_cluster_ids
                or cluster.get("person_id") is not None
                or cluster.get("suggested_person_id") is not None
                or int(cluster.get("session_count") or 0) < 2
            ):
                continue
            session_ids = tuple(str(value) for value in cluster.get("session_ids", ()))
            created_at = str(cluster["created_at"])
            items.append(
                {
                    "review_id": f"voice_identity:{cluster_id}:discovery",
                    "kind": "voice_identity",
                    "priority": "normal",
                    "source_id": cluster_id,
                    "source_revision": int(cluster["revision"]),
                    "session_id": (
                        str(cluster["latest_session_id"])
                        if cluster.get("latest_session_id") is not None
                        else None
                    ),
                    "person_id": None,
                    "title": "发现可能的新人物",
                    "summary": (
                        f"同一未知声音已在 {int(cluster['session_count'])} 次录音中出现"
                    ),
                    "reason": "recurring_unknown_speaker_requires_confirmation",
                    "evidence_count": int(cluster.get("track_count") or 0),
                    "created_at": created_at,
                    "updated_at": str(cluster["updated_at"]),
                    "context": {
                        "voice_mode": "speaker_discovery",
                        "review_lane": "primary",
                        "cluster_id": cluster_id,
                        "session_ids": list(session_ids),
                        "session_count": int(cluster["session_count"]),
                        "track_count": int(cluster.get("track_count") or 0),
                    },
                }
            )

        priority = {"high": 0, "normal": 1}
        items.sort(
            key=lambda item: (
                priority[item["priority"]],
                item["created_at"],
                item["review_id"],
            )
        )
        return tuple(items[:limit])


def _proposal_copy(
    kind: str, payload: dict[str, Any]
) -> tuple[str, str, str | None, str | None, float | None]:
    session_id = payload.get("session_id")
    if kind == "event_operation":
        patch = payload.get("patch") if isinstance(payload.get("patch"), dict) else {}
        title = str(patch.get("title") or payload.get("event_kind") or "事件提案")
        summary = str(patch.get("summary") or title)
        person_id = patch.get("person_id")
        confidence = _number(patch.get("confidence"))
    else:
        content = (
            payload.get("content") if isinstance(payload.get("content"), dict) else {}
        )
        title = str(content.get("title") or content.get("summary") or "记忆提案")
        summary = str(content.get("summary") or content.get("text") or title)
        person_id = (
            payload.get("subject_id")
            if payload.get("subject_type") == "person"
            else None
        )
        confidence = _number(content.get("confidence"))
    return (
        title,
        summary,
        str(session_id) if session_id is not None else None,
        str(person_id) if person_id is not None else None,
        confidence,
    )


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _numbers(values: Any) -> tuple[float, ...]:
    return tuple(number for value in values if (number := _number(value)) is not None)
