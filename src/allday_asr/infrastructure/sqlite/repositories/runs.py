from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import Any

from allday_asr.infrastructure.sqlite.source_fingerprint import (
    canonical_source_fingerprint,
)

from .sessions import SessionRepository
from .types import Clock, ConnectionFactory


class RunRepository:
    def __init__(
        self,
        connect: ConnectionFactory,
        *,
        sessions: SessionRepository,
        now: Clock,
        get_recording: Callable[[int], sqlite3.Row],
    ) -> None:
        self.connect = connect
        self._sessions = sessions
        self._now = now
        self._get_recording = get_recording

    def start_processing_run(
        self,
        recording_id: int | None,
        *,
        session_id: int | None = None,
        run_kind: str,
        config: dict[str, Any],
        config_sha256: str,
        model_manifest: dict[str, Any] | None = None,
        pipeline_version: str | None = None,
        code_version: str | None = None,
        parent_run_id: int | None = None,
    ) -> int:
        if recording_id is None and session_id is None:
            raise ValueError("处理运行必须指定 recording_id 或 session_id")
        if recording_id is not None:
            self._get_recording(recording_id)
        if session_id is None:
            session = self._sessions.get_session_for_recording(int(recording_id))
            session_id = int(session["id"])
        else:
            session = self._sessions.get_recording_session(session_id)
            legacy_recording_id = session["legacy_recording_id"]
            if (
                recording_id is not None
                and legacy_recording_id is not None
                and int(legacy_recording_id) != recording_id
            ):
                raise ValueError("recording_id 与 session_id 不属于同一录音会话")
        inputs = self._sessions.list_session_sources(int(session["id"]))
        input_fingerprint = canonical_source_fingerprint(inputs)
        model_manifest = model_manifest or {}
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO processing_runs (
                    recording_id, session_id, run_kind, status, config_json,
                    config_sha256, started_at, input_fingerprint,
                    model_manifest_json, pipeline_version, code_version, parent_run_id
                ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recording_id,
                    session_id,
                    run_kind,
                    json.dumps(config, ensure_ascii=False, sort_keys=True),
                    config_sha256,
                    self._now(),
                    input_fingerprint,
                    json.dumps(model_manifest, ensure_ascii=False, sort_keys=True),
                    pipeline_version,
                    code_version,
                    parent_run_id,
                ),
            )
            run_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO processing_run_inputs (
                    run_id, position, source_object_id, source_instance_id,
                    source_sha256,
                    session_start_ms, session_end_ms, source_start_ms, source_end_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        position,
                        row["source_object_id"],
                        row["source_instance_id"],
                        row["sha256"],
                        row["session_start_ms"],
                        row["session_end_ms"],
                        row["source_start_ms"],
                        row["source_end_ms"],
                    )
                    for position, row in enumerate(inputs)
                ],
            )
            return run_id

    def finish_processing_run(
        self,
        run_id: int,
        *,
        status: str,
        summary: dict[str, Any] | None = None,
        artifacts: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE processing_runs
                SET status = ?, completed_at = ?, error = ?,
                    summary_json = ?, artifacts_json = ?
                WHERE id = ?
                """,
                (
                    status,
                    self._now(),
                    error[:2000] if error else None,
                    json.dumps(summary, ensure_ascii=False) if summary is not None else None,
                    json.dumps(artifacts, ensure_ascii=False) if artifacts is not None else None,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"处理运行 {run_id} 不存在")

    def update_processing_run_progress(
        self, run_id: int, summary: dict[str, Any]
    ) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE processing_runs
                SET summary_json = ?
                WHERE id = ? AND status = 'running'
                """,
                (json.dumps(summary, ensure_ascii=False, sort_keys=True), run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("只能更新运行中的处理任务进度")

    def resume_processing_run(self, run_id: int) -> sqlite3.Row:
        run = self.get_processing_run(run_id)
        if str(run["run_kind"]) != "quality_asr_v2c":
            raise ValueError("only V2-C ASR runs can be resumed by this command")
        if str(run["status"]) == "completed":
            return run
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE processing_runs
                SET status = 'running', completed_at = NULL, error = NULL
                WHERE id = ?
                """,
                (run_id,),
            )
        return self.get_processing_run(run_id)

    def list_processing_runs(self, recording_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM processing_runs
                    WHERE recording_id = ? ORDER BY id
                    """,
                    (recording_id,),
                )
            )

    def list_session_processing_runs(self, session_id: int) -> list[sqlite3.Row]:
        self._sessions.get_recording_session(session_id)
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM processing_runs
                    WHERE session_id = ? ORDER BY id
                    """,
                    (session_id,),
                )
            )

    def list_processing_run_inputs(self, run_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM processing_run_inputs
                    WHERE run_id = ? ORDER BY position
                    """,
                    (run_id,),
                )
            )

    def get_processing_run(self, run_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM processing_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"处理运行 {run_id} 不存在")
        return row
