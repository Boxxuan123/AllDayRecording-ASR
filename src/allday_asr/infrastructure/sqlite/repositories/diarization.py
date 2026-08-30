from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from typing import Any

from .runs import RunRepository
from .sessions import SessionRepository
from .types import Clock, ConnectionFactory


class DiarizationRepository:
    def __init__(
        self,
        connect: ConnectionFactory,
        *,
        runs: RunRepository,
        sessions: SessionRepository,
        now: Clock,
    ) -> None:
        self.connect = connect
        self._runs = runs
        self._sessions = sessions
        self._now = now

    def create_diarization_turns(
        self, run_id: int, session_id: int, turns: Sequence[dict[str, Any]]
    ) -> list[sqlite3.Row]:
        """Append overlap-aware, source-traced speaker turns to a running V2-D run."""
        run = self._runs.get_processing_run(run_id)
        session = self._sessions.get_recording_session(session_id)
        if str(run["run_kind"]) != "quality_diarization_v2d":
            raise ValueError("speaker turns require a V2-D diarization run")
        if str(run["status"]) != "running":
            raise ValueError("speaker turns can only be appended to a running run")
        if int(run["session_id"]) != session_id:
            raise ValueError("speaker turn session does not match its processing run")
        duration_ms = int(session["duration_ms"])
        now = self._now()
        created_ids: list[int] = []
        with self.connect() as connection:
            for turn in turns:
                start_ms = int(turn["session_start_ms"])
                end_ms = int(turn["session_end_ms"])
                if start_ms < 0 or end_ms <= start_ms or end_ms > duration_ms:
                    raise ValueError("speaker turn falls outside its recording session")
                source_refs = list(turn.get("source_refs", []))
                if not source_refs:
                    raise ValueError("speaker turn has no immutable source reference")
                canonical_payload = {
                    "turn_key": str(turn["turn_key"]),
                    "run_id": run_id,
                    "session_id": session_id,
                    "turn_index": int(turn["turn_index"]),
                    "turn_kind": str(turn["turn_kind"]),
                    "speaker_label": str(turn["speaker_label"]),
                    "session_start_ms": start_ms,
                    "session_end_ms": end_ms,
                    "confidence": turn.get("confidence"),
                    "metadata": turn.get("metadata", {}),
                    "source_refs": source_refs,
                }
                canonical = json.dumps(
                    canonical_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                content_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                cursor = connection.execute(
                    """
                    INSERT INTO diarization_turns (
                        turn_key, run_id, session_id, turn_index, turn_kind,
                        speaker_label, session_start_ms, session_end_ms,
                        confidence, metadata_json, content_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        turn["turn_key"],
                        run_id,
                        session_id,
                        turn["turn_index"],
                        turn["turn_kind"],
                        turn["speaker_label"],
                        start_ms,
                        end_ms,
                        turn.get("confidence"),
                        json.dumps(
                            turn.get("metadata", {}),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        content_sha256,
                        now,
                    ),
                )
                turn_id = int(cursor.lastrowid)
                created_ids.append(turn_id)
                mapped_cursor = start_ms
                mapped_ms = 0
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
                        raise ValueError("speaker turn references a source outside its session")
                    if len(source_matches) > 1:
                        raise ValueError("speaker turn source instance is ambiguous")
                    source = source_matches[0]
                    source_instance_id = int(source["source_instance_id"])
                    if str(source["sha256"]) != str(source_ref["source_sha256"]):
                        raise ValueError("speaker turn source SHA-256 mismatch")
                    source_start = int(source_ref["source_start_ms"])
                    source_end = int(source_ref["source_end_ms"])
                    if (
                        source_start < int(source["source_start_ms"])
                        or source_end > int(source["source_end_ms"])
                        or source_end <= source_start
                    ):
                        raise ValueError("speaker turn has an invalid source range")
                    mapped_start = int(source["session_start_ms"]) + (
                        source_start - int(source["source_start_ms"])
                    )
                    mapped_end = mapped_start + (source_end - source_start)
                    if mapped_start != mapped_cursor or mapped_end > end_ms:
                        raise ValueError("speaker turn source references are not continuous")
                    mapped_cursor = mapped_end
                    mapped_ms += source_end - source_start
                    connection.execute(
                        """
                        INSERT INTO diarization_turn_sources (
                            turn_id, position, source_object_id,
                            source_instance_id, source_sha256,
                            source_start_ms, source_end_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            turn_id,
                            position,
                            source_ref["source_object_id"],
                            source_instance_id,
                            source_ref["source_sha256"],
                            source_start,
                            source_end,
                        ),
                    )
                if mapped_ms != end_ms - start_ms or mapped_cursor != end_ms:
                    raise ValueError("speaker turn source mapping is incomplete")
        return self.list_diarization_turns(run_id) if created_ids else []

    def get_diarization_turn(self, turn_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM diarization_turns WHERE id = ?", (turn_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"说话人时间段 {turn_id} 不存在")
        return row

    def list_diarization_turns(
        self, run_id: int, *, turn_kind: str | None = None
    ) -> list[sqlite3.Row]:
        self._runs.get_processing_run(run_id)
        sql = "SELECT * FROM diarization_turns WHERE run_id = ?"
        params: list[Any] = [run_id]
        if turn_kind is not None:
            sql += " AND turn_kind = ?"
            params.append(turn_kind)
        sql += " ORDER BY session_start_ms, session_end_ms, speaker_label, id"
        with self.connect() as connection:
            return list(connection.execute(sql, params))

    def list_diarization_turn_sources(self, turn_id: int) -> list[sqlite3.Row]:
        self.get_diarization_turn(turn_id)
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM diarization_turn_sources
                    WHERE turn_id = ? ORDER BY position
                    """,
                    (turn_id,),
                )
            )

    def create_token_speaker_attributions(
        self,
        run_id: int,
        asr_run_id: int,
        attributions: Sequence[dict[str, Any]],
    ) -> list[sqlite3.Row]:
        """Append immutable token-to-speaker decisions, including overlap and none."""
        run = self._runs.get_processing_run(run_id)
        asr_run = self._runs.get_processing_run(asr_run_id)
        if str(run["run_kind"]) != "quality_diarization_v2d":
            raise ValueError("speaker attribution requires a V2-D diarization run")
        if str(run["status"]) != "running":
            raise ValueError("speaker attribution can only be appended to a running run")
        if str(asr_run["run_kind"]) != "quality_asr_v2c":
            raise ValueError("speaker attribution requires a V2-C ASR run")
        if str(asr_run["status"]) != "completed":
            raise ValueError("speaker attribution requires a completed V2-C ASR run")
        if int(run["session_id"]) != int(asr_run["session_id"]):
            raise ValueError("diarization and ASR runs belong to different sessions")
        now = self._now()
        created_ids: list[int] = []
        with self.connect() as connection:
            for value in attributions:
                token_id = int(value["token_id"])
                token = connection.execute(
                    """
                    SELECT t.id, t.kept_in_core, h.run_id, h.hypothesis_role
                    FROM asr_alignment_tokens t
                    JOIN asr_hypotheses h ON h.id = t.hypothesis_id
                    WHERE t.id = ?
                    """,
                    (token_id,),
                ).fetchone()
                if (
                    token is None
                    or int(token["run_id"]) != asr_run_id
                    or str(token["hypothesis_role"]) != "primary"
                    or not bool(token["kept_in_core"])
                ):
                    raise ValueError("attribution token is not a committed primary ASR token")
                canonical_payload = {
                    "attribution_key": str(value["attribution_key"]),
                    "run_id": run_id,
                    "asr_run_id": asr_run_id,
                    "token_id": token_id,
                    "speaker_label": value.get("speaker_label"),
                    "attribution_kind": str(value["attribution_kind"]),
                    "overlap_ms": int(value.get("overlap_ms", 0)),
                    "overlap_ratio": float(value.get("overlap_ratio", 0.0)),
                    "rank": int(value["rank"]),
                    "confidence": value.get("confidence"),
                    "metadata": value.get("metadata", {}),
                }
                canonical = json.dumps(
                    canonical_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                content_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                cursor = connection.execute(
                    """
                    INSERT INTO token_speaker_attributions (
                        attribution_key, run_id, asr_run_id, token_id,
                        speaker_label, attribution_kind, overlap_ms,
                        overlap_ratio, rank, confidence, metadata_json,
                        content_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        value["attribution_key"],
                        run_id,
                        asr_run_id,
                        token_id,
                        value.get("speaker_label"),
                        value["attribution_kind"],
                        int(value.get("overlap_ms", 0)),
                        float(value.get("overlap_ratio", 0.0)),
                        int(value["rank"]),
                        value.get("confidence"),
                        json.dumps(
                            value.get("metadata", {}),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        content_sha256,
                        now,
                    ),
                )
                created_ids.append(int(cursor.lastrowid))
        return self.list_token_speaker_attributions(run_id) if created_ids else []

    def list_token_speaker_attributions(
        self, run_id: int, *, token_id: int | None = None
    ) -> list[sqlite3.Row]:
        self._runs.get_processing_run(run_id)
        sql = "SELECT * FROM token_speaker_attributions WHERE run_id = ?"
        params: list[Any] = [run_id]
        if token_id is not None:
            sql += " AND token_id = ?"
            params.append(token_id)
        sql += " ORDER BY token_id, rank"
        with self.connect() as connection:
            return list(connection.execute(sql, params))

    def replace_speaker_labels(
        self, recording_id: int, labels: Sequence[tuple[int, str | None]]
    ) -> None:
        now = self._now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE speech_segments
                SET speaker_session_id = NULL, person_id = NULL,
                    speaker_match_score = NULL, updated_at = ?
                WHERE recording_id = ?
                """,
                (now, recording_id),
            )
            connection.executemany(
                """
                UPDATE speech_segments
                SET speaker_session_id = ?, updated_at = ?
                WHERE id = ? AND recording_id = ?
                """,
                [(label, now, segment_id, recording_id) for segment_id, label in labels],
            )
