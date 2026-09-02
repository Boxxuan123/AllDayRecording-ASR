from __future__ import annotations
import sqlite3
from datetime import datetime
from allday_asr.v3.domain.models import (
    AudioAsset,
    AudioReplica,
    CaptureSegment,
    Device,
    RecordingSession,
    SessionManifest,
)

from .core_repository_codec import (
    _recording_session,
    _audio_asset,
    _datetime,
    _optional_datetime,
    _json,
)


class SqliteRecordingCatalogRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add_session(self, session: RecordingSession) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO recording_sessions (
                session_id, captured_start, captured_end, timezone, state,
                revision, status_code, current_stage, progress,
                blocking_reason, legacy_ref, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                session.session_id,
                _datetime(session.captured_start),
                _optional_datetime(session.captured_end),
                session.timezone,
                session.state.value,
                session.revision,
                session.status_code,
                session.current_stage,
                session.progress,
                session.blocking_reason,
                session.legacy_ref,
                _datetime(session.created_at),
                _datetime(session.updated_at),
            ),
        )
        return cursor.rowcount == 1

    def add_asset(self, asset: AudioAsset) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO audio_assets (
                asset_id, sha256, size_bytes, duration_ms, format, media_id,
                legacy_ref, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                asset.asset_id,
                asset.sha256,
                asset.size_bytes,
                asset.duration_ms,
                asset.format.value,
                asset.media_id,
                asset.legacy_ref,
                _datetime(asset.created_at),
            ),
        )
        return cursor.rowcount == 1

    def add_replica(self, replica: AudioReplica) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO audio_replicas (
                replica_id, asset_id, device_id, storage_key, state,
                verified_at, legacy_ref, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                replica.replica_id,
                replica.asset_id,
                replica.device_id,
                replica.storage_key,
                replica.state.value,
                _optional_datetime(replica.verified_at),
                replica.legacy_ref,
                _datetime(replica.created_at),
            ),
        )
        return cursor.rowcount == 1

    def add_segment(self, segment: CaptureSegment) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO capture_segments (
                segment_id, session_id, asset_id, replica_id, sequence,
                session_start_ms, session_end_ms, source_start_ms,
                source_end_ms, start_sample, captured_at, legacy_ref, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                segment.segment_id,
                segment.session_id,
                segment.asset_id,
                segment.replica_id,
                segment.sequence,
                segment.session_start_ms,
                segment.session_end_ms,
                segment.source_start_ms,
                segment.source_end_ms,
                segment.start_sample,
                _datetime(segment.captured_at),
                segment.legacy_ref,
                _datetime(segment.captured_at),
            ),
        )
        return cursor.rowcount == 1

    def add_manifest(self, manifest: SessionManifest) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO session_manifests (
                manifest_id, session_id, schema_version, sha256,
                storage_ref, entries_json, legacy_ref, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                manifest.manifest_id,
                manifest.session_id,
                manifest.schema_version,
                manifest.sha256,
                manifest.storage_ref,
                _json(manifest.entries),
                manifest.legacy_ref,
                _datetime(manifest.created_at),
            ),
        )
        return cursor.rowcount == 1

    def get_session(self, session_id: str) -> RecordingSession:
        row = self.connection.execute(
            "SELECT * FROM recording_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"recording session does not exist: {session_id}")
        return _recording_session(row)

    def find_session_by_legacy_ref(self, legacy_ref: str) -> RecordingSession | None:
        row = self.connection.execute(
            "SELECT * FROM recording_sessions WHERE legacy_ref = ?", (legacy_ref,)
        ).fetchone()
        return _recording_session(row) if row is not None else None

    def find_session_by_manifest_sha256(self, sha256: str) -> RecordingSession | None:
        row = self.connection.execute(
            """
            SELECT s.* FROM session_manifests m
            JOIN recording_sessions s ON s.session_id = m.session_id
            WHERE m.sha256 = ?
            """,
            (sha256,),
        ).fetchone()
        return _recording_session(row) if row is not None else None

    def tombstone_duplicate_session(
        self,
        session_id: str,
        canonical_session_id: str,
        tombstoned_at: datetime,
    ) -> int | None:
        timestamp = _datetime(tombstoned_at)
        reason = f"duplicate_manifest:{canonical_session_id}"
        cursor = self.connection.execute(
            """
            UPDATE recording_sessions
            SET state = 'quarantined', revision = revision + 1,
                status_code = 'stale', current_stage = 'deduplicated',
                progress = 0.0, blocking_reason = ?, tombstoned_at = ?,
                updated_at = ?
            WHERE session_id = ? AND tombstoned_at IS NULL
              AND NOT EXISTS (
                SELECT 1 FROM session_manifests m
                WHERE m.session_id = recording_sessions.session_id
              )
            """,
            (reason, timestamp, timestamp, session_id),
        )
        if cursor.rowcount != 1:
            return None
        return int(
            self.connection.execute(
                "SELECT revision FROM recording_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
        )

    def find_asset_by_sha256(self, sha256: str) -> AudioAsset | None:
        row = self.connection.execute(
            "SELECT * FROM audio_assets WHERE sha256 = ?", (sha256,)
        ).fetchone()
        return _audio_asset(row) if row is not None else None


class SqliteDeviceRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add(self, device: Device) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO devices (
                device_id, kind, name, status, revision, last_seen_at,
                legacy_ref, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                device.device_id,
                device.kind.value,
                device.name,
                device.status.value,
                device.revision,
                _optional_datetime(device.last_seen_at),
                device.legacy_ref,
                _datetime(device.created_at),
                _datetime(device.updated_at),
            ),
        )
        return cursor.rowcount == 1
