from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

from allday_asr.infrastructure.sqlite.source_fingerprint import (
    canonical_source_fingerprint,
)

from .types import Clock, ConnectionFactory


class SessionRepository:
    def __init__(
        self,
        connect: ConnectionFactory,
        *,
        now: Clock,
        get_recording: Callable[[int], sqlite3.Row],
    ) -> None:
        self.connect = connect
        self._now = now
        self._get_recording = get_recording

    def get_recording_session(self, session_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM recording_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"录音会话 {session_id} 不存在")
        return row

    def find_recording_session(self, session_key: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM recording_sessions WHERE session_key = ?", (session_key,)
            ).fetchone()

    def get_session_manifest(self, session_id: int) -> sqlite3.Row | None:
        self.get_recording_session(session_id)
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM session_manifests WHERE session_id = ?", (session_id,)
            ).fetchone()

    def find_session_manifest_by_hash(self, manifest_sha256: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM session_manifests WHERE manifest_sha256 = ?",
                (manifest_sha256,),
            ).fetchone()

    def get_session_for_recording(self, recording_id: int) -> sqlite3.Row:
        self._get_recording(recording_id)
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM recording_sessions
                WHERE legacy_recording_id = ?
                """,
                (recording_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"录音 {recording_id} 尚未映射到 V2 会话")
        return row

    def list_recording_sessions(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(connection.execute("SELECT * FROM recording_sessions ORDER BY id"))

    def create_recording_session(self, values: dict[str, Any]) -> sqlite3.Row:
        now = self._now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO recording_sessions (
                    session_key, legacy_recording_id, device, recorded_at,
                    timezone, duration_ms, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["session_key"],
                    values.get("legacy_recording_id"),
                    values.get("device"),
                    values["recorded_at"],
                    values["timezone"],
                    values["duration_ms"],
                    values.get("status", "active"),
                    now,
                    now,
                ),
            )
            session_id = int(cursor.lastrowid)
            return connection.execute(
                "SELECT * FROM recording_sessions WHERE id = ?", (session_id,)
            ).fetchone()

    def list_session_sources(self, session_id: int) -> list[sqlite3.Row]:
        self.get_recording_session(session_id)
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT ss.*, so.sha256,
                           si.source_path AS source_path,
                           si.original_filename AS original_filename,
                           si.byte_size AS instance_byte_size,
                           si.instance_key,
                           si.integrity_status,
                           so.duration_ms AS source_duration_ms
                    FROM session_sources ss
                    JOIN source_objects so ON so.id = ss.source_object_id
                    JOIN source_instances si ON si.id = ss.source_instance_id
                    WHERE ss.session_id = ?
                    ORDER BY ss.chunk_index
                    """,
                    (session_id,),
                )
            )

    def session_input_fingerprint(self, session_id: int) -> str:
        rows = self.list_session_sources(session_id)
        if not rows:
            raise RuntimeError(f"录音会话 {session_id} 没有原始音频对象")
        return canonical_source_fingerprint(rows)
