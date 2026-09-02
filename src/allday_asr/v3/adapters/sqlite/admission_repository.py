from __future__ import annotations
import sqlite3
from allday_asr.v3.domain.models import RecordingSessionState
from allday_asr.v3.domain.processing import (
    BackupEvidence,
)
from .repositories import Clock

from .processing_repository_codec import _datetime, _optional_datetime, _json


class SqliteAdmissionRepository:
    def __init__(self, connection: sqlite3.Connection, *, now: Clock) -> None:
        self.connection = connection
        self.now = now

    def add_evidence(self, evidence: BackupEvidence) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO backup_evidence (
                evidence_id, session_id, provider, storage_kind, digest, status,
                restore_checked_at, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING
            """,
            (
                evidence.evidence_id,
                evidence.session_id,
                evidence.provider,
                evidence.storage_kind,
                evidence.digest,
                evidence.status.value,
                _optional_datetime(evidence.restore_checked_at),
                _json(evidence.metadata),
                _datetime(evidence.created_at),
            ),
        )
        return cursor.rowcount == 1

    def evaluate(self, session_id: str) -> tuple[bool, str | None]:
        session = self.connection.execute(
            "SELECT state FROM recording_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if session is None:
            raise KeyError(f"recording session does not exist: {session_id}")
        manifest_count = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM session_manifests WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
        )
        segments = self.connection.execute(
            """
            SELECT r.state FROM capture_segments s
            JOIN audio_replicas r ON r.replica_id = s.replica_id
            WHERE s.session_id = ?
            """,
            (session_id,),
        ).fetchall()
        evidence = self.connection.execute(
            """
            SELECT 1 FROM backup_evidence WHERE session_id = ? AND status = 'verified'
              AND restore_checked_at IS NOT NULL LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        if manifest_count != 1:
            return False, "session_manifest_missing"
        if not segments:
            return False, "capture_segments_missing"
        if any(row["state"] != "available" for row in segments):
            return False, "computer_audio_replica_unavailable"
        if evidence is None:
            return False, "backup_restore_evidence_required"
        return True, None

    def apply(self, session_id: str, admitted: bool, reason: str | None) -> int:
        now = self.now()
        row = self.connection.execute(
            "SELECT revision FROM recording_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"recording session does not exist: {session_id}")
        revision = int(row["revision"]) + 1
        self.connection.execute(
            """
            UPDATE recording_sessions SET state = ?, revision = ?, status_code = ?,
                current_stage = 'backup_admitted', progress = ?, blocking_reason = ?,
                updated_at = ? WHERE session_id = ?
            """,
            (
                RecordingSessionState.READY_FOR_PROCESSING.value
                if admitted
                else RecordingSessionState.ADMISSION_BLOCKED.value,
                revision,
                "ready" if admitted else "backup_required",
                1.0 if admitted else 0.0,
                None if admitted else reason,
                now,
                session_id,
            ),
        )
        return revision
