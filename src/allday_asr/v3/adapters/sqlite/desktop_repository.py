from __future__ import annotations

import json
import sqlite3
from typing import Any

from allday_asr.v3.adapters.sqlite.people_repository import SqlitePeopleRepository
from allday_asr.v3.adapters.sqlite.reminder_repository import SqliteReminderRepository
from allday_asr.v3.contracts import validate_utterance_dto


class SqliteDesktopReadRepository:
    """Read-only product projections for the loopback Desktop API."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def overview(self) -> dict[str, Any]:
        counts = self.connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM recording_sessions
                WHERE tombstoned_at IS NULL) AS sessions,
              (SELECT COUNT(*) FROM processing_jobs WHERE status IN
                ('queued', 'running', 'cancel_requested')) AS active_jobs,
              (SELECT COUNT(*) FROM processing_jobs WHERE status =
                'failed_retryable') AS retryable_jobs,
              (SELECT COUNT(*) FROM devices WHERE status = 'active') AS active_devices,
              (SELECT COALESCE(SUM(size_bytes), 0) FROM audio_assets) AS audio_bytes,
              (SELECT COUNT(DISTINCT session_id) FROM backup_evidence
                WHERE status = 'verified') AS backed_up_sessions
            """
        ).fetchone()
        recent = self.list_sessions(None, None, 6)
        jobs = self.list_processing_jobs(None, 6)
        return {
            "counts": {key: int(counts[key]) for key in counts.keys()},
            "recent_sessions": list(recent),
            "active_jobs": [
                value
                for value in jobs
                if value["status"] in {"queued", "running", "cancel_requested"}
            ],
        }

    def list_sessions(
        self,
        before_captured_start: str | None,
        before_session_id: str | None,
        limit: int,
    ) -> tuple[dict[str, Any], ...]:
        where = "WHERE s.tombstoned_at IS NULL"
        parameters: list[object] = []
        if before_captured_start is not None and before_session_id is not None:
            where += (
                " AND (s.captured_start < ? OR "
                "(s.captured_start = ? AND s.session_id > ?))"
            )
            parameters.extend(
                [before_captured_start, before_captured_start, before_session_id]
            )
        parameters.append(limit)
        rows = self.connection.execute(
            f"""
            SELECT s.*,
              COUNT(DISTINCT seg.segment_id) AS segment_count,
              COALESCE(MAX(seg.session_end_ms), 0) AS duration_ms,
              (
                SELECT a.media_id FROM capture_segments first_seg
                JOIN audio_assets a ON a.asset_id = first_seg.asset_id
                WHERE first_seg.session_id = s.session_id
                ORDER BY first_seg.sequence, first_seg.segment_id LIMIT 1
              ) AS media_id,
              (
                SELECT p.run_id FROM processing_runs p
                WHERE p.session_id = s.session_id
                ORDER BY p.created_at DESC, p.run_id DESC LIMIT 1
              ) AS latest_run_id,
              (
                SELECT p.status FROM processing_runs p
                WHERE p.session_id = s.session_id
                ORDER BY p.created_at DESC, p.run_id DESC LIMIT 1
              ) AS processing_status
            FROM recording_sessions s
            LEFT JOIN capture_segments seg ON seg.session_id = s.session_id
            {where}
            GROUP BY s.session_id
            ORDER BY s.captured_start DESC, s.session_id
            LIMIT ?
            """,
            parameters,
        ).fetchall()
        return tuple(_session(row) for row in rows)

    def session_detail(self, session_id: str) -> dict[str, Any]:
        session_rows = self.connection.execute(
            "SELECT captured_start, session_id FROM recording_sessions "
            "WHERE session_id = ? AND tombstoned_at IS NULL",
            (session_id,),
        ).fetchone()
        if session_rows is None:
            raise KeyError(f"recording session does not exist: {session_id}")
        # Read the selected row directly; keyset pagination intentionally excludes it.
        row = self.connection.execute(
            """
            SELECT s.*,
              COUNT(DISTINCT seg.segment_id) AS segment_count,
              COALESCE(MAX(seg.session_end_ms), 0) AS duration_ms,
              (SELECT a.media_id FROM capture_segments x JOIN audio_assets a
                ON a.asset_id = x.asset_id WHERE x.session_id = s.session_id
                ORDER BY x.sequence, x.segment_id LIMIT 1) AS media_id,
              (SELECT p.run_id FROM processing_runs p WHERE p.session_id = s.session_id
                ORDER BY p.created_at DESC, p.run_id DESC LIMIT 1) AS latest_run_id,
              (SELECT p.status FROM processing_runs p WHERE p.session_id = s.session_id
                ORDER BY p.created_at DESC, p.run_id DESC LIMIT 1) AS processing_status
            FROM recording_sessions s
            LEFT JOIN capture_segments seg ON seg.session_id = s.session_id
            WHERE s.session_id = ? GROUP BY s.session_id
            """,
            (session_id,),
        ).fetchone()
        assert row is not None
        segments = self.connection.execute(
            """
            SELECT seg.segment_id, seg.sequence, seg.session_start_ms,
              seg.session_end_ms, seg.source_start_ms, seg.source_end_ms,
              a.asset_id, a.media_id, a.sha256,
              a.size_bytes, a.duration_ms, a.format, r.replica_id,
              r.state AS replica_state, d.name AS device_name
            FROM capture_segments seg
            JOIN audio_assets a ON a.asset_id = seg.asset_id
            JOIN audio_replicas r ON r.replica_id = seg.replica_id
            LEFT JOIN devices d ON d.device_id = r.device_id
            WHERE seg.session_id = ? ORDER BY seg.sequence, seg.segment_id
            """,
            (session_id,),
        ).fetchall()
        runs = self.connection.execute(
            """
            SELECT p.*, j.job_id, j.status AS job_status
            FROM processing_runs p LEFT JOIN processing_jobs j ON j.run_id = p.run_id
            WHERE p.session_id = ? ORDER BY p.created_at DESC, p.run_id DESC
            """,
            (session_id,),
        ).fetchall()
        timeline_run_id = self._canonical_timeline_run_id(session_id)
        if timeline_run_id is None:
            utterances = []
            speaker_tracks = []
        else:
            utterances = self.connection.execute(
                """
                SELECT u.*, t.label AS speaker_label,
                  original.label AS original_speaker_label FROM utterances u
                LEFT JOIN speaker_tracks t ON t.speaker_track_id = u.speaker_track_id
                LEFT JOIN speaker_tracks original
                  ON original.speaker_track_id = u.original_speaker_track_id
                WHERE u.run_id = ? AND u.status = 'active'
                ORDER BY u.start_ms, u.end_ms, u.utterance_id
                """,
                (timeline_run_id,),
            ).fetchall()
            speaker_tracks = self.connection.execute(
                """
                SELECT t.speaker_track_id, t.session_id, t.label,
                  t.source_artifact_id, t.created_at,
                  m.cluster_id AS speaker_cluster_id,
                  l.person_id, p.display_name AS person_name
                FROM speaker_tracks t
                LEFT JOIN speaker_cluster_memberships m
                  ON m.speaker_track_id = t.speaker_track_id AND m.state = 'active'
                LEFT JOIN person_cluster_links l
                  ON l.cluster_id = m.cluster_id AND l.status = 'active'
                LEFT JOIN persons p ON p.person_id = l.person_id
                WHERE t.session_id = ? AND (
                  t.run_id = ? OR EXISTS (
                    SELECT 1 FROM utterances u
                    WHERE u.run_id = ? AND u.status = 'active'
                      AND (u.speaker_track_id = t.speaker_track_id
                        OR u.original_speaker_track_id = t.speaker_track_id)
                  )
                )
                ORDER BY t.label, t.speaker_track_id
                """,
                (session_id, timeline_run_id, timeline_run_id),
            ).fetchall()
        artifacts = self.connection.execute(
            """
            SELECT a.*, CASE WHEN EXISTS (
              SELECT 1 FROM artifact_status_events e WHERE e.artifact_id = a.artifact_id
            ) THEN 'stale' ELSE a.status END AS effective_status
            FROM artifacts a JOIN processing_runs p ON p.run_id = a.run_id
            WHERE p.session_id = ? ORDER BY a.created_at DESC, a.artifact_id
            """,
            (session_id,),
        ).fetchall()
        backups = self.connection.execute(
            "SELECT * FROM backup_evidence WHERE session_id = ? ORDER BY created_at DESC",
            (session_id,),
        ).fetchall()
        return {
            "session": _session(row),
            "segments": [_dict(value) for value in segments],
            "runs": [_run(value) for value in runs],
            "speaker_tracks": [_dict(value) for value in speaker_tracks],
            "utterances": [_utterance(value) for value in utterances],
            "artifacts": [_artifact(value) for value in artifacts],
            "backups": [_backup(value) for value in backups],
        }

    def _canonical_timeline_run_id(self, session_id: str) -> str | None:
        row = self.connection.execute(
            """
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
            """,
            (session_id,),
        ).fetchone()
        return str(row["run_id"]) if row is not None else None

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

        reminders = SqliteReminderRepository(self.connection).list_candidates(
            "pending_confirmation", limit
        )
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

        voice_candidates = SqlitePeopleRepository(
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

    def list_devices(self) -> tuple[dict[str, Any], ...]:
        rows = self.connection.execute(
            """
            SELECT d.*,
              EXISTS(SELECT 1 FROM device_credentials c
                WHERE c.device_id = d.device_id AND c.revoked_at IS NULL) AS paired,
              (SELECT receiver_id FROM pairing_records p
                WHERE p.device_id = d.device_id ORDER BY paired_at DESC LIMIT 1) AS receiver_id
            FROM devices d ORDER BY d.kind, d.name, d.device_id
            """
        ).fetchall()
        return tuple(_device(row) for row in rows)

    def data_health(self) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM audio_assets) AS audio_assets,
              (SELECT COALESCE(SUM(size_bytes), 0) FROM audio_assets) AS audio_bytes,
              (SELECT COUNT(*) FROM audio_replicas WHERE state = 'available') AS available_replicas,
              (SELECT COUNT(*) FROM audio_replicas
                WHERE state IN ('failed_retryable', 'quarantined')) AS unhealthy_replicas,
              (SELECT COUNT(*) FROM backup_evidence WHERE status = 'verified') AS verified_backups,
              (SELECT COUNT(*) FROM artifacts) AS artifacts,
              (SELECT COALESCE(SUM(size_bytes), 0) FROM artifacts) AS artifact_bytes,
              (SELECT COUNT(*) FROM artifact_status_events WHERE status = 'stale') AS stale_artifacts
            """
        ).fetchone()
        return {key: int(row[key]) for key in row.keys()}

    def media(self, media_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT a.media_id, a.size_bytes, a.format, r.storage_key
            FROM audio_assets a JOIN audio_replicas r ON r.asset_id = a.asset_id
            WHERE a.media_id = ? AND r.state = 'available'
            ORDER BY r.verified_at DESC, r.replica_id LIMIT 1
            """,
            (media_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"media does not exist: {media_id}")
        return _dict(row)

    def processing_events(
        self, after_sequence: int, limit: int
    ) -> tuple[dict[str, Any], ...]:
        rows = self.connection.execute(
            """
            SELECT sequence, resource_id, revision, operation, payload_json, created_at
            FROM change_events WHERE sequence > ? AND resource_type = 'processing_run'
            ORDER BY sequence LIMIT ?
            """,
            (after_sequence, limit),
        ).fetchall()
        return tuple(
            {
                **_dict(row),
                "payload": _json_object(row["payload_json"]),
            }
            for row in rows
        )


def _session(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "session_id": str(row["session_id"]),
        "captured_start": str(row["captured_start"]),
        "captured_end": row["captured_end"],
        "timezone": str(row["timezone"]),
        "state": str(row["state"]),
        "revision": int(row["revision"]),
        "status_code": str(row["status_code"]),
        "current_stage": row["current_stage"],
        "progress": float(row["progress"]),
        "updated_at": str(row["updated_at"]),
        "blocking_reason": row["blocking_reason"],
        "legacy_ref": row["legacy_ref"],
        "segment_count": int(row["segment_count"]),
        "duration_ms": int(row["duration_ms"]),
        "media_id": row["media_id"],
        "latest_run_id": row["latest_run_id"],
        "processing_status": row["processing_status"],
    }


def _run(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "run_id": str(row["run_id"]),
        "job_id": row["job_id"],
        "session_id": str(row["session_id"]),
        "pipeline_version": str(row["pipeline_version"]),
        "input_revision": int(row["input_revision"]),
        "revision": int(row["revision"]),
        "status": str(row["status"]),
        "job_status": row["job_status"],
        "current_stage": row["current_stage"],
        "progress": float(row["progress"]),
        "error": row["error"],
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "completed_at": row["completed_at"],
    }


def _utterance(row: sqlite3.Row) -> dict[str, Any]:
    return validate_utterance_dto({
        "utterance_id": str(row["utterance_id"]),
        "session_id": str(row["session_id"]),
        "speaker_track_id": row["speaker_track_id"],
        "speaker_label": row["speaker_label"],
        "original_speaker_track_id": row["original_speaker_track_id"],
        "original_speaker_label": row["original_speaker_label"],
        "identity": str(row["identity"]),
        "original_identity": str(row["original_identity"]),
        "identity_evidence": _json_object(row["identity_evidence_json"]),
        "start_ms": int(row["start_ms"]),
        "end_ms": int(row["end_ms"]),
        "start_at": str(row["start_at"]),
        "end_at": str(row["end_at"]),
        "text": str(row["text"]),
        "original_text": str(row["original_text"]),
        "revision": int(row["revision"]),
        "status": str(row["status"]),
        "evidence": _json_object(row["evidence_json"]),
    })


def _artifact(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "artifact_id": str(row["artifact_id"]),
        "run_id": str(row["run_id"]),
        "kind": str(row["kind"]),
        "producer": str(row["producer"]),
        "producer_version": str(row["producer_version"]),
        "status": str(row["effective_status"]),
        "sha256": str(row["sha256"]) if row["sha256"] is not None else None,
        "size_bytes": (
            int(row["size_bytes"]) if row["size_bytes"] is not None else None
        ),
        "metadata": _json_object(row["metadata_json"]),
        "created_at": str(row["created_at"]),
    }


def _backup(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "evidence_id": str(row["evidence_id"]),
        "provider": str(row["provider"]),
        "storage_kind": str(row["storage_kind"]),
        "digest": str(row["digest"]),
        "status": str(row["status"]),
        "restore_checked_at": row["restore_checked_at"],
        "metadata": _json_object(row["metadata_json"]),
        "created_at": str(row["created_at"]),
    }


def _job(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "job_id": str(row["job_id"]),
        "run_id": str(row["run_id"]),
        "session_id": str(row["session_id"]),
        "pipeline_version": str(row["pipeline_version"]),
        "input_revision": int(row["input_revision"]),
        "revision": int(row["revision"]),
        "status": str(row["status"]),
        "priority": int(row["priority"]),
        "current_stage": row["current_stage"],
        "progress": float(row["progress"]),
        "stage_count": int(row["stage_count"]),
        "completed_stage_count": int(row["completed_stage_count"]),
        "error": row["error"],
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "completed_at": row["completed_at"],
    }


def _device(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "device_id": str(row["device_id"]),
        "kind": str(row["kind"]),
        "name": str(row["name"]),
        "status": str(row["status"]),
        "revision": int(row["revision"]),
        "last_seen_at": row["last_seen_at"],
        "updated_at": str(row["updated_at"]),
        "paired": bool(row["paired"]),
        "receiver_id": row["receiver_id"],
    }


def _dict(row: sqlite3.Row) -> dict[str, Any]:
    return {str(key): row[key] for key in row.keys()}


def _json_object(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    parsed = json.loads(str(value))
    return parsed if isinstance(parsed, dict) else {}


__all__ = ["SqliteDesktopReadRepository"]
