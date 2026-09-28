from __future__ import annotations
import base64
import binascii
import json
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
    def timeline_page(
        self, session_id: str, limit: int = 80, cursor: str | None = None,
        search: str | None = None, speaker: str | None = None,
        start_ms: int | None = None, end_ms: int | None = None,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if self.connection.execute(
            "SELECT 1 FROM recording_sessions WHERE session_id = ? AND tombstoned_at IS NULL",
            (session_id,),
        ).fetchone() is None:
            raise KeyError(f"recording session does not exist: {session_id}")
        run_id = self._canonical_timeline_run_id(session_id)
        if run_id is None:
            return {"items": [], "next_cursor": None, "total": 0, "speakers": [], "speaker_tracks": []}
        conditions = ["u.run_id = ?", "u.status = 'active'"]
        params: list[Any] = [run_id]
        if search:
            escaped = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            if escaped:
                conditions.append("(u.text LIKE ? ESCAPE '\\' OR u.start_at LIKE ? ESCAPE '\\' "
                                  "OR t.label LIKE ? ESCAPE '\\' OR EXISTS ("
                                  "SELECT 1 FROM speaker_cluster_memberships sm "
                                  "JOIN person_cluster_links pl ON pl.cluster_id=sm.cluster_id AND pl.status='active' "
                                  "JOIN persons pn ON pn.person_id=pl.person_id "
                                  "WHERE sm.speaker_track_id=u.speaker_track_id AND sm.state='active' "
                                  "AND pn.display_name LIKE ? ESCAPE '\\') OR "
                                  "printf('%02d:%02d:%02d', u.start_ms/3600000, "
                                  "(u.start_ms/60000)%60, (u.start_ms/1000)%60) LIKE ? ESCAPE '\\')")
                params.extend([f"%{escaped}%"] * 5)
        if speaker and speaker != "all":
            conditions.append("COALESCE(t.label, 'unassigned') = ?")
            params.append(speaker)
        if start_ms is not None:
            conditions.append("u.end_ms > ?")
            params.append(start_ms)
        if end_ms is not None:
            conditions.append("u.start_ms < ?")
            params.append(end_ms)
        from_clause = "FROM utterances u LEFT JOIN speaker_tracks t ON t.speaker_track_id = u.speaker_track_id"
        where_clause = " AND ".join(conditions)
        total = self.connection.execute(
            f"SELECT COUNT(*) {from_clause} WHERE {where_clause}", params,
        ).fetchone()[0]
        page_conditions = list(conditions)
        page_params = list(params)
        if cursor:
            try:
                position = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                if not (isinstance(position, list) and len(position) == 3
                        and type(position[0]) is int and type(position[1]) is int
                        and isinstance(position[2], str)):
                    raise ValueError
            except (ValueError, TypeError, UnicodeDecodeError, binascii.Error) as exc:
                raise ValueError("invalid timeline cursor") from exc
            page_conditions.append("(u.start_ms, u.end_ms, u.utterance_id) > (?, ?, ?)")
            page_params.extend(position)
        rows = self.connection.execute(
            f"""SELECT u.utterance_id, u.session_id, u.speaker_track_id,
              u.original_speaker_track_id, u.identity, u.original_identity,
              u.start_ms, u.end_ms, u.start_at, u.end_at, u.text,
              u.original_text, u.revision, u.status,
              json_extract(u.evidence_json, '$.sound_kind') AS sound_kind,
              t.label AS speaker_label, original.label AS original_speaker_label,
              person.display_name AS person_name
              {from_clause}
              LEFT JOIN speaker_tracks original ON original.speaker_track_id = u.original_speaker_track_id
              LEFT JOIN speaker_cluster_memberships membership
                ON membership.speaker_track_id = t.speaker_track_id AND membership.state = 'active'
              LEFT JOIN person_cluster_links link
                ON link.cluster_id = membership.cluster_id AND link.status = 'active'
              LEFT JOIN persons person ON person.person_id = link.person_id
              WHERE {' AND '.join(page_conditions)}
              GROUP BY u.utterance_id
              ORDER BY u.start_ms, u.end_ms, u.utterance_id LIMIT ?""",
            (*page_params, limit + 1),
        ).fetchall()
        page = rows[:limit]
        items = [{
            "utterance_id": row["utterance_id"], "session_id": row["session_id"],
            "speaker_track_id": row["speaker_track_id"], "speaker_label": row["speaker_label"],
            "person_name": row["person_name"],
            "original_speaker_track_id": row["original_speaker_track_id"],
            "original_speaker_label": row["original_speaker_label"],
            "identity": row["identity"], "original_identity": row["original_identity"],
            "identity_evidence": {}, "start_ms": row["start_ms"], "end_ms": row["end_ms"],
            "start_at": row["start_at"], "end_at": row["end_at"],
            "text": row["text"], "original_text": row["original_text"],
            "revision": row["revision"], "status": row["status"],
            "evidence": {"sound_kind": row["sound_kind"]} if row["sound_kind"] else {},
        } for row in page]
        next_cursor = None
        if len(rows) > limit and page:
            last = page[-1]
            payload = json.dumps([last["start_ms"], last["end_ms"], last["utterance_id"]], separators=(",", ":"))
            next_cursor = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        return {"items": items, "next_cursor": next_cursor, "total": total,
                "speakers": [], "speaker_tracks": []}

    def timeline_speakers(self, session_id: str) -> tuple[dict[str, Any], ...]:
        if self.connection.execute(
            "SELECT 1 FROM recording_sessions WHERE session_id = ? AND tombstoned_at IS NULL",
            (session_id,),
        ).fetchone() is None:
            raise KeyError(f"recording session does not exist: {session_id}")
        run_id = self._canonical_timeline_run_id(session_id)
        if run_id is None:
            return ()
        speaker_rows = self.connection.execute(
            """SELECT COALESCE(t.label, 'unassigned') AS label, COUNT(DISTINCT u.utterance_id) AS count,
              MIN(u.speaker_track_id) AS speaker_track_id,
              MIN(person.display_name) AS person_name
              FROM utterances u LEFT JOIN speaker_tracks t ON t.speaker_track_id = u.speaker_track_id
              LEFT JOIN speaker_cluster_memberships membership
                ON membership.speaker_track_id = t.speaker_track_id AND membership.state = 'active'
              LEFT JOIN person_cluster_links link
                ON link.cluster_id = membership.cluster_id AND link.status = 'active'
              LEFT JOIN persons person ON person.person_id = link.person_id
              WHERE u.run_id = ? AND u.status = 'active'
              GROUP BY COALESCE(t.label, 'unassigned') ORDER BY label""",
            (run_id,),
        ).fetchall()
        return tuple(_dict(row) for row in speaker_rows)

    def timeline_utterance(self, session_id: str, utterance_id: str) -> dict[str, Any]:
        run_id = self._canonical_timeline_run_id(session_id)
        row = self.connection.execute(
            """SELECT u.*, t.label AS speaker_label,
              original.label AS original_speaker_label
              FROM utterances u
              LEFT JOIN speaker_tracks t ON t.speaker_track_id = u.speaker_track_id
              LEFT JOIN speaker_tracks original
                ON original.speaker_track_id = u.original_speaker_track_id
              WHERE u.session_id = ? AND u.run_id = ?
                AND u.utterance_id = ? AND u.status = 'active'""",
            (session_id, run_id, utterance_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"utterance does not exist: {utterance_id}")
        return _utterance(row)

    def timeline_speaker_tracks(self, session_id: str) -> tuple[dict[str, Any], ...]:
        if self.connection.execute(
            "SELECT 1 FROM recording_sessions WHERE session_id = ? AND tombstoned_at IS NULL",
            (session_id,),
        ).fetchone() is None:
            raise KeyError(f"recording session does not exist: {session_id}")
        run_id = self._canonical_timeline_run_id(session_id)
        if run_id is None:
            return ()
        tracks = self.connection.execute(
            """SELECT t.speaker_track_id, t.label, MIN(p.display_name) AS person_name
              FROM speaker_tracks t
              LEFT JOIN speaker_cluster_memberships m ON m.speaker_track_id = t.speaker_track_id AND m.state = 'active'
              LEFT JOIN person_cluster_links l ON l.cluster_id = m.cluster_id AND l.status = 'active'
              LEFT JOIN persons p ON p.person_id = l.person_id
              WHERE t.session_id = ? AND t.run_id = ?
              GROUP BY t.speaker_track_id ORDER BY t.label, t.speaker_track_id""",
            (session_id, run_id),
        ).fetchall()
        return tuple(_dict(row) for row in tracks)

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
        search: str | None = None,
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
        if search is not None:
            normalized = search.casefold().replace("/", "-")
            escaped = (
                normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            pattern = f"%{escaped}%"
            where += """
              AND (
                LOWER(s.session_id) LIKE ? ESCAPE '\\'
                OR LOWER(s.captured_start) LIKE ? ESCAPE '\\'
                OR LOWER(s.timezone) LIKE ? ESCAPE '\\'
                OR EXISTS (
                  SELECT 1 FROM utterances search_u
                  LEFT JOIN speaker_tracks search_t
                    ON search_t.speaker_track_id = search_u.speaker_track_id
                  LEFT JOIN speaker_cluster_memberships search_m
                    ON search_m.speaker_track_id = search_t.speaker_track_id
                    AND search_m.state = 'active'
                  LEFT JOIN person_cluster_links search_l
                    ON search_l.cluster_id = search_m.cluster_id
                    AND search_l.status = 'active'
                  LEFT JOIN persons search_p ON search_p.person_id = search_l.person_id
                  WHERE search_u.session_id = s.session_id
                    AND search_u.status = 'active'
                    AND (
                      LOWER(search_u.text) LIKE ? ESCAPE '\\'
                      OR LOWER(COALESCE(search_t.label, '')) LIKE ? ESCAPE '\\'
                      OR LOWER(COALESCE(search_p.display_name, '')) LIKE ? ESCAPE '\\'
                    )
                )
              )
            """
            parameters.extend([pattern] * 6)
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

    def session_detail(self, session_id: str, include_timeline: bool = True,
                       include_history: bool = True,
                       segment_ranges_only: bool = False) -> dict[str, Any]:
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
        ).fetchall() if not segment_ranges_only else []
        segment_ranges = self.connection.execute(
            """SELECT session_start_ms, session_end_ms FROM capture_segments
               WHERE session_id = ? ORDER BY sequence, segment_id""", (session_id,)
        ).fetchall() if segment_ranges_only else []
        runs = self.connection.execute(
            """
            SELECT p.*, j.job_id, j.status AS job_status
            FROM processing_runs p LEFT JOIN processing_jobs j ON j.run_id = p.run_id
            WHERE p.session_id = ? ORDER BY p.created_at DESC, p.run_id DESC
            """,
            (session_id,),
        ).fetchall() if include_history else []
        timeline_run_id = self._canonical_timeline_run_id(session_id)
        utterance_count = (
            self.connection.execute(
                "SELECT COUNT(*) FROM utterances WHERE run_id = ? AND status = 'active'",
                (timeline_run_id,),
            ).fetchone()[0]
            if timeline_run_id is not None else 0
        )
        if timeline_run_id is None or not include_timeline:
            utterances = []
            speaker_tracks = []
        else:
            utterances = self.connection.execute(
                """
                SELECT u.*, CASE WHEN t.label LIKE 'manual:%' THEN COALESCE((
                  SELECT p.display_name FROM speaker_cluster_memberships m
                  JOIN person_cluster_links l ON l.cluster_id = m.cluster_id AND l.status = 'active'
                  JOIN persons p ON p.person_id = l.person_id
                  WHERE m.speaker_track_id = t.speaker_track_id AND m.state = 'active' LIMIT 1
                ), t.label) ELSE t.label END AS speaker_label,
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
        ).fetchall() if include_history else []
        backups = self.connection.execute(
            "SELECT * FROM backup_evidence WHERE session_id = ? ORDER BY created_at DESC",
            (session_id,),
        ).fetchall() if include_history else []
        return {
            "session": _session(row),
            "segments": [_dict(value) for value in segments],
            "segment_ranges": [_dict(value) for value in segment_ranges],
            "runs": [_run(value) for value in runs],
            "speaker_tracks": [_dict(value) for value in speaker_tracks],
            "utterances": [_utterance(value) for value in utterances],
            "utterance_count": utterance_count,
            "artifacts": [_artifact(value) for value in artifacts],
            "backups": [_backup(value) for value in backups],
        }

    def session_audio_segments(
        self, session_id: str, start_ms: int, end_ms: int
    ) -> tuple[dict[str, Any], ...]:
        session = self.connection.execute(
            "SELECT 1 FROM recording_sessions "
            "WHERE session_id = ? AND tombstoned_at IS NULL",
            (session_id,),
        ).fetchone()
        if session is None:
            raise KeyError(f"recording session does not exist: {session_id}")
        rows = self.connection.execute(
            """
            SELECT seg.segment_id, seg.sequence, seg.session_start_ms,
              seg.session_end_ms, seg.source_start_ms, seg.source_end_ms,
              a.media_id, replica.storage_key
            FROM capture_segments seg
            JOIN audio_assets a ON a.asset_id = seg.asset_id
            JOIN audio_replicas replica ON replica.replica_id = COALESCE(
              (SELECT captured.replica_id
               FROM audio_replicas captured
               WHERE captured.replica_id = seg.replica_id
                 AND captured.state = 'available'),
              (SELECT candidate.replica_id
               FROM audio_replicas candidate
               WHERE candidate.asset_id = seg.asset_id
                 AND candidate.state = 'available'
               ORDER BY candidate.verified_at DESC, candidate.replica_id
               LIMIT 1)
            )
            WHERE seg.session_id = ?
              AND seg.session_end_ms > ?
              AND seg.session_start_ms < ?
            ORDER BY seg.session_start_ms, seg.sequence, seg.segment_id
            """,
            (session_id, start_ms, end_ms),
        ).fetchall()
        return tuple(_dict(row) for row in rows)

    def review_evidence_detail(
        self, session_id: str, clips: tuple[dict[str, Any], ...]
    ) -> dict[str, Any]:
        """Read only the media mapping and sentence windows used by review cards."""
        segments = [
            _dict(row) for row in self.connection.execute(
                """
                SELECT seg.session_start_ms, seg.session_end_ms,
                  seg.source_start_ms, seg.source_end_ms, a.media_id
                FROM capture_segments seg
                JOIN audio_assets a ON a.asset_id = seg.asset_id
                WHERE seg.session_id = ?
                ORDER BY seg.sequence, seg.segment_id
                """, (session_id,)
            ).fetchall()
        ]
        intervals: set[tuple[int, int]] = set()
        for clip in clips:
            media, start, end = (clip.get(key) for key in ("media_id", "start_ms", "end_ms"))
            if not isinstance(media, str) or type(start) is not int or type(end) is not int:
                continue
            matches = [seg for seg in segments if seg["media_id"] == media
                and seg["source_start_ms"] <= start and end <= seg["source_end_ms"]
                and end - seg["source_start_ms"] <= seg["session_end_ms"] - seg["session_start_ms"]]
            if len(matches) == 1:
                offset = matches[0]["session_start_ms"] - matches[0]["source_start_ms"]
                intervals.add((start + offset, end + offset))
        run_id = self._canonical_timeline_run_id(session_id)
        utterances: list[dict[str, Any]] = []
        if run_id is not None and intervals:
            selected = sorted(intervals)
            by_id: dict[str, dict[str, Any]] = {}
            for offset in range(0, len(selected), 200):
                batch = selected[offset:offset + 200]
                predicates = " OR ".join("(u.start_ms < ? AND u.end_ms > ?)" for _ in batch)
                parameters: list[object] = [run_id]
                for start, end in batch:
                    parameters.extend((end, start))
                for row in self.connection.execute(
                    f"""SELECT u.utterance_id, u.revision, u.session_id,
                      u.status, u.start_ms, u.end_ms FROM utterances u
                      WHERE u.run_id = ? AND u.status = 'active'
                        AND ({predicates})""",
                    parameters,
                ).fetchall():
                    value = _dict(row)
                    by_id[str(value["utterance_id"])] = value
            utterances = sorted(by_id.values(), key=lambda row: (
                row["start_ms"], row["end_ms"], row["utterance_id"]))
        return {"segments": segments, "utterances": utterances}

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
