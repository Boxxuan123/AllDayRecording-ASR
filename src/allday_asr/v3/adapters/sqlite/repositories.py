from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.models import (
    Artifact,
    AudioAsset,
    AudioFormat,
    AudioReplica,
    CaptureSegment,
    CorrectionOperation,
    Device,
    ProcessingRun,
    ProcessingStatus,
    RecordingSession,
    RecordingSessionState,
    SessionManifest,
)


Clock = Callable[[], str]


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

    def find_session_by_legacy_ref(
        self, legacy_ref: str
    ) -> RecordingSession | None:
        row = self.connection.execute(
            "SELECT * FROM recording_sessions WHERE legacy_ref = ?", (legacy_ref,)
        ).fetchone()
        return _recording_session(row) if row is not None else None

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


class SqliteProcessingRunRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add(self, run: ProcessingRun) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO processing_runs (
                run_id, session_id, pipeline_version, input_revision, status,
                config_digest, current_stage, progress, completed_at, error,
                legacy_ref, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                run.run_id,
                run.session_id,
                run.pipeline_version,
                run.input_revision,
                run.status.value,
                run.config_digest,
                run.current_stage,
                run.progress,
                _optional_datetime(run.completed_at),
                run.error,
                run.legacy_ref,
                _datetime(run.created_at),
                _datetime(run.updated_at),
            ),
        )
        return cursor.rowcount == 1

    def find_by_legacy_ref(self, legacy_ref: str) -> ProcessingRun | None:
        row = self.connection.execute(
            "SELECT * FROM processing_runs WHERE legacy_ref = ?", (legacy_ref,)
        ).fetchone()
        return _processing_run(row) if row is not None else None


class SqliteArtifactRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add(self, artifact: Artifact) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO artifacts (
                artifact_id, run_id, kind, producer, producer_version,
                config_digest, input_refs_json, storage_ref, sha256,
                size_bytes, status, metadata_json, legacy_ref, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                artifact.artifact_id,
                artifact.run_id,
                artifact.kind,
                artifact.producer,
                artifact.producer_version,
                artifact.config_digest,
                _json(list(artifact.input_refs)),
                artifact.storage_ref,
                artifact.sha256,
                artifact.size_bytes,
                artifact.status,
                _json(artifact.metadata),
                artifact.legacy_ref,
                _datetime(artifact.created_at),
            ),
        )
        return cursor.rowcount == 1


class SqliteCorrectionRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add(self, correction: CorrectionOperation) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO correction_operations (
                correction_id, target_type, target_id, before_revision,
                patch_json, actor, legacy_ref, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                correction.correction_id,
                correction.target_type,
                correction.target_id,
                correction.before_revision,
                _json(correction.patch),
                correction.actor,
                correction.legacy_ref,
                _datetime(correction.created_at),
            ),
        )
        return cursor.rowcount == 1


class SqliteChangeLogRepository:
    def __init__(self, connection: sqlite3.Connection, *, now: Clock) -> None:
        self.connection = connection
        self.now = now

    def append(
        self,
        resource_type: str,
        resource_id: str,
        revision: int,
        operation: str,
        payload: dict[str, Any] | None,
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO change_events (
                resource_type, resource_id, revision, operation,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                resource_type,
                resource_id,
                revision,
                operation,
                _json(payload) if payload is not None else None,
                self.now(),
            ),
        )
        return int(cursor.lastrowid)


class SqliteAuditRepository:
    def __init__(self, connection: sqlite3.Connection, *, now: Clock) -> None:
        self.connection = connection
        self.now = now

    def append(
        self,
        action: str,
        actor: str,
        target_type: str,
        target_id: str,
        details: dict[str, Any],
        *,
        legacy_ref: str | None = None,
    ) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO audit_entries (
                audit_id, action, actor, target_type, target_id,
                details_json, legacy_ref, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                new_ulid(),
                action,
                actor,
                target_type,
                target_id,
                _json(details),
                legacy_ref,
                self.now(),
            ),
        )
        return cursor.rowcount == 1


class SqliteIdempotencyRepository:
    def __init__(self, connection: sqlite3.Connection, *, now: Clock) -> None:
        self.connection = connection
        self.now = now

    def begin(self, key: str, command: str) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO idempotency_records (
                idempotency_key, command, status, created_at
            ) VALUES (?, ?, 'started', ?)
            ON CONFLICT DO NOTHING
            """,
            (key, command, self.now()),
        )
        if cursor.rowcount == 1:
            return True
        row = self.connection.execute(
            "SELECT command FROM idempotency_records WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        if row is not None and row["command"] != command:
            raise ValueError("idempotency key was already used for another command")
        return False

    def complete(self, key: str, response: dict[str, Any]) -> None:
        cursor = self.connection.execute(
            """
            UPDATE idempotency_records
            SET status = 'completed', response_json = ?, completed_at = ?
            WHERE idempotency_key = ? AND status = 'started'
            """,
            (_json(response), self.now(), key),
        )
        if cursor.rowcount != 1:
            raise LookupError(f"idempotency operation is not active: {key}")

    def response(self, key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT status, response_json FROM idempotency_records "
            "WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        if row is None or row["status"] != "completed":
            return None
        return json.loads(str(row["response_json"]))


class SqliteTombstoneRepository:
    def __init__(self, connection: sqlite3.Connection, *, now: Clock) -> None:
        self.connection = connection
        self.now = now

    def add(
        self,
        resource_type: str,
        resource_id: str,
        revision: int,
        reason: str | None,
    ) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO tombstones (
                resource_type, resource_id, revision, reason, deleted_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (resource_type, resource_id, revision, reason, self.now()),
        )
        return cursor.rowcount == 1


class SqliteLegacyImportRunRepository:
    def __init__(self, connection: sqlite3.Connection, *, now: Clock) -> None:
        self.connection = connection
        self.now = now

    def start(
        self,
        import_id: str,
        source_namespace: str,
        source_path: str,
        source_database_sha256: str,
        source_schema_version: int,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO legacy_import_runs (
                import_id, source_namespace, source_path,
                source_database_sha256, source_schema_version,
                status, started_at
            ) VALUES (?, ?, ?, ?, ?, 'running', ?)
            """,
            (
                import_id,
                source_namespace,
                source_path,
                source_database_sha256,
                source_schema_version,
                self.now(),
            ),
        )

    def complete(self, import_id: str, report: dict[str, Any]) -> None:
        self._finish(import_id, "completed", report)

    def fail(self, import_id: str, report: dict[str, Any]) -> None:
        self._finish(import_id, "failed", report)

    def _finish(self, import_id: str, status: str, report: dict[str, Any]) -> None:
        cursor = self.connection.execute(
            """
            UPDATE legacy_import_runs
            SET status = ?, report_json = ?, completed_at = ?
            WHERE import_id = ? AND status = 'running'
            """,
            (status, _json(report), self.now(), import_id),
        )
        if cursor.rowcount != 1:
            raise LookupError(f"legacy import run is not active: {import_id}")


def _recording_session(row: sqlite3.Row) -> RecordingSession:
    return RecordingSession(
        session_id=str(row["session_id"]),
        captured_start=_parse_datetime(row["captured_start"]),
        captured_end=_optional_parse_datetime(row["captured_end"]),
        timezone=str(row["timezone"]),
        state=RecordingSessionState(str(row["state"])),
        revision=int(row["revision"]),
        status_code=str(row["status_code"]),
        current_stage=row["current_stage"],
        progress=float(row["progress"]),
        blocking_reason=row["blocking_reason"],
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
        legacy_ref=row["legacy_ref"],
    )


def _audio_asset(row: sqlite3.Row) -> AudioAsset:
    return AudioAsset(
        asset_id=str(row["asset_id"]),
        sha256=str(row["sha256"]),
        size_bytes=int(row["size_bytes"]),
        duration_ms=int(row["duration_ms"]),
        format=AudioFormat(str(row["format"])),
        media_id=str(row["media_id"]),
        created_at=_parse_datetime(row["created_at"]),
        legacy_ref=row["legacy_ref"],
    )


def _processing_run(row: sqlite3.Row) -> ProcessingRun:
    return ProcessingRun(
        run_id=str(row["run_id"]),
        session_id=str(row["session_id"]),
        pipeline_version=str(row["pipeline_version"]),
        input_revision=int(row["input_revision"]),
        status=ProcessingStatus(str(row["status"])),
        config_digest=str(row["config_digest"]),
        current_stage=row["current_stage"],
        progress=float(row["progress"]),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
        completed_at=_optional_parse_datetime(row["completed_at"]),
        error=row["error"],
        legacy_ref=row["legacy_ref"],
    )


def _datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("database timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _optional_datetime(value: datetime | None) -> str | None:
    return _datetime(value) if value is not None else None


def _parse_datetime(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("stored timestamp is not timezone-aware")
    return parsed.astimezone(timezone.utc)


def _optional_parse_datetime(value: object) -> datetime | None:
    return _parse_datetime(value) if value is not None else None


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "SqliteArtifactRepository",
    "SqliteAuditRepository",
    "SqliteChangeLogRepository",
    "SqliteCorrectionRepository",
    "SqliteDeviceRepository",
    "SqliteIdempotencyRepository",
    "SqliteLegacyImportRunRepository",
    "SqliteProcessingRunRepository",
    "SqliteRecordingCatalogRepository",
    "SqliteTombstoneRepository",
    "utc_now",
]
