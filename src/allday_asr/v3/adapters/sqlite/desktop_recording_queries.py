from __future__ import annotations
from typing import Any

from .desktop_repository_codec import (
    _session,
    _run,
    _utterance,
    _artifact,
    _backup,
    _dict,
)


class DesktopRecordingQueryMixin:
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
