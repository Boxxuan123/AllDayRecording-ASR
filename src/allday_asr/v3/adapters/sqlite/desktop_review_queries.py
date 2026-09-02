from __future__ import annotations
from typing import Any
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
        stage_rows = self.connection.execute(
            """
            SELECT p.run_id, p.session_id, j.job_id, s.stage_run_id, s.stage,
              s.error, s.created_at, s.updated_at
            FROM stage_runs s JOIN processing_runs p ON p.run_id = s.run_id
            LEFT JOIN processing_jobs j ON j.run_id = p.run_id
            WHERE s.status = 'waiting_review'
            ORDER BY s.updated_at, s.stage_run_id LIMIT ?
            """,
            (limit,),
        ).fetchall()
        for row in stage_rows:
            stage = str(row["stage"])
            items.append(
                {
                    "review_id": f"processing_gate:{row['stage_run_id']}",
                    "kind": "processing_gate",
                    "priority": "high",
                    "source_id": str(row["stage_run_id"]),
                    "source_revision": None,
                    "session_id": str(row["session_id"]),
                    "person_id": None,
                    "title": stage,
                    "summary": str(row["error"] or stage),
                    "reason": "processing_stage_waiting_review",
                    "evidence_count": 0,
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["updated_at"]),
                    "context": {
                        "run_id": str(row["run_id"]),
                        "job_id": row["job_id"],
                        "stage_run_id": str(row["stage_run_id"]),
                        "stage": stage,
                        "error": row["error"],
                    },
                }
            )

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

        memory_rows = self.connection.execute(
            """
            SELECT current.memory_id, current.revision, current.person_id,
              current.kind, current.summary, current.confidence,
              current.confirmation_status, current.event_id, current.created_at,
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
                    "reason": "person_memory_unconfirmed",
                    "evidence_count": int(row["evidence_count"]),
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["created_at"]),
                    "context": {
                        "memory_kind": str(row["kind"]),
                        "confirmation_status": str(row["confirmation_status"]),
                        "confidence": float(row["confidence"]),
                        "event_id": row["event_id"],
                    },
                }
            )

        voice_candidates = compatibility.SqlitePeopleRepository(
            self.connection
        ).list_review_candidates(None, "pending", limit)
        for candidate in voice_candidates:
            clips = candidate.get("representative_clips", ())
            items.append(
                {
                    "review_id": f"voice_identity:{candidate['prototype_id']}:{candidate['person_id']}",
                    "kind": "voice_identity",
                    "priority": "normal",
                    "source_id": str(candidate["prototype_id"]),
                    "source_revision": None,
                    "session_id": str(candidate["session_id"]),
                    "person_id": str(candidate["person_id"]),
                    "title": str(candidate["person_name"]),
                    "summary": str(candidate.get("match_reason") or "voice_identity"),
                    "reason": "voice_identity_requires_confirmation",
                    "evidence_count": len(clips),
                    "created_at": str(candidate["created_at"]),
                    "updated_at": str(
                        (candidate.get("review") or {}).get("created_at")
                        or candidate["created_at"]
                    ),
                    "context": {
                        "cluster_id": str(candidate["cluster_id"]),
                        "speaker_track_id": str(candidate["speaker_track_id"]),
                        "quality_score": float(candidate["quality_score"]),
                        "decision_tier": candidate.get("decision_tier"),
                        "best_score": candidate.get("best_score"),
                        "score_margin": candidate.get("score_margin"),
                        "review_status": str(candidate["review_status"]),
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
