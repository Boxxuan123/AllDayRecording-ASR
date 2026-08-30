"""ASR hypothesis, token, disagreement, and legacy segment persistence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Sequence

from allday_asr.infrastructure.sqlite.repositories.runs import RunRepository
from allday_asr.infrastructure.sqlite.repositories.types import Clock, ConnectionFactory


class AsrRepository:
    """Persist immutable V2-C ASR artifacts and legacy ASR segment state."""

    def __init__(
        self,
        connect: ConnectionFactory,
        *,
        runs: RunRepository,
        now: Clock,
    ) -> None:
        self.connect = connect
        self._runs = runs
        self._now = now

    def create_asr_hypothesis(
        self, values: dict[str, Any], tokens: Sequence[dict[str, Any]]
    ) -> sqlite3.Row:
        """Append one immutable model hypothesis and its source-traced tokens."""
        run_id = int(values["run_id"])
        run = self._runs.get_processing_run(run_id)
        session_id = int(values["session_id"])
        if int(run["session_id"]) != session_id:
            raise ValueError("ASR hypothesis session does not match its processing run")
        if str(run["status"]) != "running":
            raise ValueError("ASR hypotheses can only be appended to a running processing run")

        analysis_start = int(values["analysis_start_ms"])
        analysis_end = int(values["analysis_end_ms"])
        core_start = int(values["core_start_ms"])
        core_end = int(values["core_end_ms"])
        if not (
            0 <= analysis_start <= core_start < core_end <= analysis_end
        ):
            raise ValueError("ASR hypothesis has an invalid window range")

        canonical_payload = {
            "hypothesis_key": values["hypothesis_key"],
            "run_id": run_id,
            "session_id": session_id,
            "window_index": int(values["window_index"]),
            "hypothesis_role": values["hypothesis_role"],
            "core_start_ms": core_start,
            "core_end_ms": core_end,
            "analysis_start_ms": analysis_start,
            "analysis_end_ms": analysis_end,
            "model_id": values["model_id"],
            "model_revision": values.get("model_revision"),
            "backend": values["backend"],
            "language": values.get("language"),
            "text": values.get("text", ""),
            "parameters": values.get("parameters", {}),
            "raw_response": values.get("raw_response", {}),
            "tokens": list(tokens),
        }
        canonical_json = json.dumps(
            canonical_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        content_sha256 = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        now = self._now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO asr_hypotheses (
                    hypothesis_key, run_id, session_id, window_index,
                    hypothesis_role, core_start_ms, core_end_ms,
                    analysis_start_ms, analysis_end_ms, model_id,
                    model_revision, backend, language, text, parameters_json,
                    raw_response_json, content_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["hypothesis_key"],
                    run_id,
                    session_id,
                    values["window_index"],
                    values["hypothesis_role"],
                    core_start,
                    core_end,
                    analysis_start,
                    analysis_end,
                    values["model_id"],
                    values.get("model_revision"),
                    values["backend"],
                    values.get("language"),
                    values.get("text", ""),
                    json.dumps(values.get("parameters", {}), ensure_ascii=False, sort_keys=True),
                    json.dumps(values.get("raw_response", {}), ensure_ascii=False, sort_keys=True),
                    content_sha256,
                    now,
                ),
            )
            hypothesis_id = int(cursor.lastrowid)
            for token_index, token in enumerate(tokens):
                token_start = int(token["session_start_ms"])
                token_end = int(token["session_end_ms"])
                if token_start < analysis_start or token_end > analysis_end or token_end <= token_start:
                    raise ValueError(f"ASR token {token_index} falls outside its analysis window")
                token_cursor = connection.execute(
                    """
                    INSERT INTO asr_alignment_tokens (
                        hypothesis_id, token_index, text, session_start_ms,
                        session_end_ms, analysis_start_ms, analysis_end_ms,
                        kept_in_core, confidence, alignment_model_id,
                        metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        hypothesis_id,
                        int(token.get("token_index", token_index)),
                        token["text"],
                        token_start,
                        token_end,
                        int(token["analysis_start_ms"]),
                        int(token["analysis_end_ms"]),
                        1 if token.get("kept_in_core", False) else 0,
                        token.get("confidence"),
                        token.get("alignment_model_id"),
                        json.dumps(token.get("metadata", {}), ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )
                token_id = int(token_cursor.lastrowid)
                source_refs = list(token.get("source_refs", []))
                if not source_refs:
                    raise ValueError(f"ASR token {token_index} has no immutable source reference")
                mapped_ms = 0
                mapped_cursor = token_start
                for position, source_ref in enumerate(source_refs):
                    source_instance_id = source_ref.get("source_instance_id")
                    source_matches = list(connection.execute(
                        """
                        SELECT so.sha256, ss.source_instance_id,
                               ss.session_start_ms, ss.session_end_ms,
                               ss.source_start_ms, ss.source_end_ms
                        FROM source_objects so
                        JOIN session_sources ss ON ss.source_object_id = so.id
                        WHERE so.id = ? AND ss.session_id = ?
                          AND (? IS NULL OR ss.source_instance_id = ?)
                        """,
                        (
                            source_ref["source_object_id"],
                            session_id,
                            source_instance_id,
                            source_instance_id,
                        ),
                    ))
                    if not source_matches:
                        raise ValueError("ASR token references a source outside its session")
                    if len(source_matches) > 1:
                        raise ValueError("ASR token source instance is ambiguous")
                    source = source_matches[0]
                    source_instance_id = int(source["source_instance_id"])
                    if str(source["sha256"]) != str(source_ref["source_sha256"]):
                        raise ValueError("ASR token source SHA-256 mismatch")
                    source_start = int(source_ref["source_start_ms"])
                    source_end = int(source_ref["source_end_ms"])
                    if (
                        source_start < int(source["source_start_ms"])
                        or source_end > int(source["source_end_ms"])
                        or source_end <= source_start
                    ):
                        raise ValueError("ASR token has an invalid source range")
                    mapped_start = int(source["session_start_ms"]) + (
                        source_start - int(source["source_start_ms"])
                    )
                    mapped_end = mapped_start + (source_end - source_start)
                    if mapped_start != mapped_cursor or mapped_end > token_end:
                        raise ValueError("ASR token source references are not continuous")
                    mapped_cursor = mapped_end
                    mapped_ms += source_end - source_start
                    connection.execute(
                        """
                        INSERT INTO asr_token_sources (
                            token_id, position, source_object_id,
                            source_instance_id, source_sha256,
                            source_start_ms, source_end_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            token_id,
                            position,
                            source_ref["source_object_id"],
                            source_instance_id,
                            source_ref["source_sha256"],
                            source_start,
                            source_end,
                        ),
                    )
                if mapped_ms != token_end - token_start or mapped_cursor != token_end:
                    raise ValueError(f"ASR token {token_index} source mapping is incomplete")
        return self.get_asr_hypothesis(hypothesis_id)

    def get_asr_hypothesis(self, hypothesis_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM asr_hypotheses WHERE id = ?", (hypothesis_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"ASR hypothesis {hypothesis_id} does not exist")
        return row

    def list_asr_hypotheses(
        self, run_id: int, *, role: str | None = None
    ) -> list[sqlite3.Row]:
        self._runs.get_processing_run(run_id)
        sql = "SELECT * FROM asr_hypotheses WHERE run_id = ?"
        params: list[Any] = [run_id]
        if role is not None:
            sql += " AND hypothesis_role = ?"
            params.append(role)
        sql += " ORDER BY window_index, hypothesis_role"
        with self.connect() as connection:
            return list(connection.execute(sql, params))

    def list_asr_tokens(
        self, hypothesis_id: int, *, core_only: bool = False
    ) -> list[sqlite3.Row]:
        self.get_asr_hypothesis(hypothesis_id)
        sql = "SELECT * FROM asr_alignment_tokens WHERE hypothesis_id = ?"
        params: list[Any] = [hypothesis_id]
        if core_only:
            sql += " AND kept_in_core = 1"
        sql += " ORDER BY token_index"
        with self.connect() as connection:
            return list(connection.execute(sql, params))

    def list_asr_token_sources(self, token_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM asr_token_sources WHERE token_id = ? ORDER BY position",
                    (token_id,),
                )
            )

    def list_asr_token_sources_for_run(self, run_id: int) -> list[sqlite3.Row]:
        """Return source coordinates for every committed primary token in a run."""
        self._runs.get_processing_run(run_id)
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT s.*, t.session_start_ms, t.session_end_ms
                    FROM asr_token_sources s
                    JOIN asr_alignment_tokens t ON t.id = s.token_id
                    JOIN asr_hypotheses h ON h.id = t.hypothesis_id
                    WHERE h.run_id = ? AND h.hypothesis_role = 'primary'
                      AND t.kept_in_core = 1
                    ORDER BY t.session_start_ms, t.session_end_ms,
                             t.id, s.position
                    """,
                    (run_id,),
                )
            )

    def list_committed_asr_tokens(self, run_id: int) -> list[sqlite3.Row]:
        """Return immutable primary, core-only tokens for one completed ASR run."""
        self._runs.get_processing_run(run_id)
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT t.*, h.run_id AS asr_run_id, h.window_index,
                           h.model_id, h.hypothesis_key
                    FROM asr_alignment_tokens t
                    JOIN asr_hypotheses h ON h.id = t.hypothesis_id
                    WHERE h.run_id = ? AND h.hypothesis_role = 'primary'
                      AND t.kept_in_core = 1
                    ORDER BY t.session_start_ms, t.session_end_ms, t.id
                    """,
                    (run_id,),
                )
            )

    def create_asr_disagreement(self, values: dict[str, Any]) -> sqlite3.Row:
        now = self._now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO asr_disagreements (
                    run_id, window_index, primary_hypothesis_id,
                    secondary_hypothesis_id, session_start_ms, session_end_ms,
                    normalized_distance, priority, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["run_id"],
                    values["window_index"],
                    values["primary_hypothesis_id"],
                    values["secondary_hypothesis_id"],
                    values["session_start_ms"],
                    values["session_end_ms"],
                    values["normalized_distance"],
                    values["priority"],
                    json.dumps(values.get("details", {}), ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            disagreement_id = int(cursor.lastrowid)
            return connection.execute(
                "SELECT * FROM asr_disagreements WHERE id = ?", (disagreement_id,)
            ).fetchone()

    def list_asr_disagreements(self, run_id: int) -> list[sqlite3.Row]:
        self._runs.get_processing_run(run_id)
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM asr_disagreements
                    WHERE run_id = ?
                    ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                             window_index
                    """,
                    (run_id,),
                )
            )

    def segment_count(self, recording_id: int) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM speech_segments WHERE recording_id = ?",
                (recording_id,),
            ).fetchone()
            return int(row["count"])

    def replace_vad_segments(
        self,
        recording_id: int,
        segments: Sequence[tuple[int, int]],
        source_path: str,
    ) -> None:
        now = self._now()
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM speech_segments WHERE recording_id = ?", (recording_id,)
            )
            connection.executemany(
                """
                INSERT INTO speech_segments (
                    recording_id, segment_index, start_ms, end_ms, audio_ref,
                    asr_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                [
                    (
                        recording_id,
                        index,
                        start_ms,
                        end_ms,
                        f"{source_path}#t={start_ms / 1000:.3f},{end_ms / 1000:.3f}",
                        now,
                        now,
                    )
                    for index, (start_ms, end_ms) in enumerate(segments)
                ],
            )

    def reset_failed_segments(self, recording_id: int) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE speech_segments
                SET asr_status = 'pending', error = NULL, updated_at = ?
                WHERE recording_id = ? AND asr_status = 'failed'
                """,
                (self._now(), recording_id),
            )
            return cursor.rowcount

    def reset_interrupted_segments(self, recording_id: int) -> int:
        """Recover segments left in running state by an interrupted process."""
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE speech_segments
                SET asr_status = 'pending', error = '上次处理被中断，已自动恢复', updated_at = ?
                WHERE recording_id = ? AND asr_status = 'running'
                """,
                (self._now(), recording_id),
            )
            return cursor.rowcount

    def reset_all_asr_segments(self, recording_id: int) -> int:
        """Explicitly discard derived ASR text while preserving VAD boundaries."""
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE speech_segments
                SET asr_status = 'pending', language = NULL, text_raw = NULL,
                    text_display = NULL, asr_model = NULL, error = NULL, updated_at = ?
                WHERE recording_id = ?
                """,
                (self._now(), recording_id),
            )
            return cursor.rowcount

    def pending_segments(
        self, recording_id: int, limit: int | None = None
    ) -> list[sqlite3.Row]:
        sql = """
            SELECT * FROM speech_segments
            WHERE recording_id = ? AND asr_status = 'pending'
            ORDER BY segment_index
        """
        params: list[Any] = [recording_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as connection:
            return list(connection.execute(sql, params))

    def all_segments(self, recording_id: int, completed_only: bool = False) -> list[sqlite3.Row]:
        sql = """
            SELECT speech_segments.*, person_profiles.display_name AS person_name
            FROM speech_segments
            LEFT JOIN person_profiles ON person_profiles.id = speech_segments.person_id
            WHERE speech_segments.recording_id = ?
        """
        if completed_only:
            sql += " AND speech_segments.asr_status = 'completed'"
        sql += " ORDER BY speech_segments.segment_index"
        with self.connect() as connection:
            return list(connection.execute(sql, (recording_id,)))

    def mark_segment_running(self, segment_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE speech_segments
                SET asr_status = 'running', attempts = attempts + 1, error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (self._now(), segment_id),
            )

    def mark_segment_completed(
        self,
        segment_id: int,
        *,
        language: str | None,
        text_raw: str,
        text_display: str,
        asr_model: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE speech_segments
                SET asr_status = 'completed', language = ?, text_raw = ?, text_display = ?,
                    asr_model = ?, error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (language, text_raw, text_display, asr_model, self._now(), segment_id),
            )

    def mark_segment_failed(self, segment_id: int, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE speech_segments
                SET asr_status = 'failed', error = ?, updated_at = ?
                WHERE id = ?
                """,
                (error[:2000], self._now(), segment_id),
            )

    def segment_status_counts(self, recording_id: int) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT asr_status, COUNT(*) AS count
                FROM speech_segments WHERE recording_id = ? GROUP BY asr_status
                """,
                (recording_id,),
            )
            return {row["asr_status"]: int(row["count"]) for row in rows}
