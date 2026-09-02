from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from allday_asr.v3.domain.device_sync import (
    ClientOperation,
    ClientOperationRecord,
    ClientOperationStatus,
    DeviceCredential,
    DeviceScope,
    OperationReceipt,
    PairingRecord,
)
from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.models import (
    Artifact,
    AudioAsset,
    AudioFormat,
    AudioReplica,
    CaptureSegment,
    ChangeEvent,
    ChangeOperation,
    CorrectionOperation,
    Device,
    ProcessingRun,
    ProcessingStatus,
    RecordingSession,
    RecordingSessionState,
    SessionManifest,
)
from allday_asr.v3.domain.processing import ArtifactInvalidation


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

    def get(self, run_id: str) -> ProcessingRun:
        row = self.connection.execute(
            "SELECT * FROM processing_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"processing run does not exist: {run_id}")
        return _processing_run(row)


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

    def list_active_for_run(self, run_id: str) -> tuple[Artifact, ...]:
        rows = self.connection.execute(
            """
            SELECT a.* FROM artifacts a
            WHERE a.run_id = ? AND a.status = 'active'
              AND NOT EXISTS (
                SELECT 1 FROM artifact_status_events e
                WHERE e.artifact_id = a.artifact_id
              )
            ORDER BY a.created_at, a.artifact_id
            """,
            (run_id,),
        ).fetchall()
        return tuple(_artifact(row) for row in rows)

    def add_dependency(
        self, artifact_id: str, input_type: str, input_id: str, input_revision: int
    ) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO artifact_dependencies (
                artifact_id, input_type, input_id, input_revision
            ) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING
            """,
            (artifact_id, input_type, input_id, input_revision),
        )
        return cursor.rowcount == 1

    def dependent_ids(self, input_type: str, input_id: str) -> tuple[str, ...]:
        return tuple(
            str(row["artifact_id"])
            for row in self.connection.execute(
                """
                SELECT DISTINCT artifact_id FROM artifact_dependencies
                WHERE input_type = ? AND input_id = ? ORDER BY artifact_id
                """,
                (input_type, input_id),
            )
        )

    def invalidate(self, event: ArtifactInvalidation) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO artifact_status_events (
                status_event_id, artifact_id, status, reason, source_type,
                source_id, source_revision, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING
            """,
            (
                event.status_event_id,
                event.artifact_id,
                event.status,
                event.reason,
                event.source_type,
                event.source_id,
                event.source_revision,
                _datetime(event.created_at),
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

    def list_for_target(
        self, target_type: str, target_id: str
    ) -> tuple[CorrectionOperation, ...]:
        rows = self.connection.execute(
            """
            SELECT * FROM correction_operations
            WHERE target_type = ? AND target_id = ?
            ORDER BY before_revision, created_at, correction_id
            """,
            (target_type, target_id),
        ).fetchall()
        return tuple(
            CorrectionOperation(
                correction_id=str(row["correction_id"]),
                target_type=str(row["target_type"]),
                target_id=str(row["target_id"]),
                before_revision=(
                    int(row["before_revision"])
                    if row["before_revision"] is not None
                    else None
                ),
                patch=_json_object(row["patch_json"]),
                actor=str(row["actor"]),
                legacy_ref=row["legacy_ref"],
                created_at=_parse_datetime(row["created_at"]),
            )
            for row in rows
        )


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

    def list_after(self, sequence: int, limit: int) -> tuple[ChangeEvent, ...]:
        if sequence < 0 or not 1 <= limit <= 501:
            raise ValueError("invalid change-log range")
        rows = self.connection.execute(
            """
            SELECT * FROM change_events
            WHERE sequence > ?
            ORDER BY sequence
            LIMIT ?
            """,
            (sequence, limit),
        )
        return tuple(_change_event(row) for row in rows)

    def latest(self, resource_type: str, resource_id: str) -> ChangeEvent | None:
        row = self.connection.execute(
            """
            SELECT * FROM change_events
            WHERE resource_type = ? AND resource_id = ?
            ORDER BY sequence DESC
            LIMIT 1
            """,
            (resource_type, resource_id),
        ).fetchone()
        return _change_event(row) if row is not None else None


class SqliteDeviceTrustRepository:
    def __init__(self, connection: sqlite3.Connection, *, now: Clock) -> None:
        self.connection = connection
        self.now = now

    def enroll(self, credential: DeviceCredential, pairing: PairingRecord) -> bool:
        current = self.find_by_key_id(credential.key_id)
        if current is not None:
            if (
                current.device_id != credential.device_id
                or current.algorithm != credential.algorithm
                or current.public_key != credential.public_key
                or current.passkey_credential_ref != credential.passkey_credential_ref
            ):
                raise ValueError("device key conflicts with an enrolled credential")
            return False
        self.connection.execute(
            """
            INSERT INTO device_credentials (
                credential_id, device_id, key_id, algorithm, public_key,
                scopes_json, passkey_credential_ref, revoked_at,
                last_used_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                credential.credential_id,
                credential.device_id,
                credential.key_id,
                credential.algorithm,
                credential.public_key,
                _json([scope.value for scope in credential.scopes]),
                credential.passkey_credential_ref,
                _optional_datetime(credential.revoked_at),
                _optional_datetime(credential.last_used_at),
                _datetime(credential.created_at),
            ),
        )
        self.connection.execute(
            """
            INSERT INTO pairing_records (
                pairing_id, device_id, receiver_id,
                passkey_credential_ref, paired_at, revoked_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                pairing.pairing_id,
                pairing.device_id,
                pairing.receiver_id,
                pairing.passkey_credential_ref,
                _datetime(pairing.paired_at),
                _optional_datetime(pairing.revoked_at),
            ),
        )
        return True

    def find_by_key_id(self, key_id: str) -> DeviceCredential | None:
        row = self.connection.execute(
            "SELECT * FROM device_credentials WHERE key_id = ?", (key_id,)
        ).fetchone()
        return _device_credential(row) if row is not None else None

    def authorize(
        self, key_id: str, required_scopes: tuple[DeviceScope, ...]
    ) -> DeviceCredential:
        credential = self.find_by_key_id(key_id)
        if credential is None or credential.revoked_at is not None:
            raise LookupError("device credential is missing or revoked")
        missing = set(required_scopes) - set(credential.scopes)
        if missing:
            values = ", ".join(sorted(scope.value for scope in missing))
            raise PermissionError(f"device credential lacks required scopes: {values}")
        return credential

    def mark_used(self, key_id: str) -> None:
        cursor = self.connection.execute(
            "UPDATE device_credentials SET last_used_at = ? "
            "WHERE key_id = ? AND revoked_at IS NULL",
            (self.now(), key_id),
        )
        if cursor.rowcount != 1:
            raise LookupError("device credential is missing or revoked")

    def revoke(self, key_id: str, revoked_at: str) -> bool:
        row = self.connection.execute(
            "SELECT device_id, revoked_at FROM device_credentials WHERE key_id = ?",
            (key_id,),
        ).fetchone()
        if row is None or row["revoked_at"] is not None:
            return False
        device_id = str(row["device_id"])
        self.connection.execute(
            "UPDATE device_credentials SET revoked_at = ? WHERE key_id = ?",
            (revoked_at, key_id),
        )
        self.connection.execute(
            "UPDATE pairing_records SET revoked_at = ? "
            "WHERE device_id = ? AND revoked_at IS NULL",
            (revoked_at, device_id),
        )
        self.connection.execute(
            """
            UPDATE devices
            SET status = 'revoked', revision = revision + 1,
                updated_at = ?, tombstoned_at = ?
            WHERE device_id = ? AND status != 'revoked'
            """,
            (revoked_at, revoked_at, device_id),
        )
        return True


class SqliteMobileSyncRepository:
    def __init__(self, connection: sqlite3.Connection, *, now: Clock) -> None:
        self.connection = connection
        self.now = now

    def find_operation(self, operation_id: str) -> ClientOperationRecord | None:
        row = self.connection.execute(
            "SELECT * FROM client_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        return _client_operation_record(row) if row is not None else None

    def record_operation(
        self,
        device_id: str,
        operation: ClientOperation,
        payload_sha256: str,
        receipt: OperationReceipt,
    ) -> bool:
        now = self.now()
        cursor = self.connection.execute(
            """
            INSERT INTO client_operations (
                operation_id, device_id, kind, base_revision,
                payload_sha256, payload_json, status, receipt_json,
                created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                operation.operation_id,
                device_id,
                operation.kind,
                operation.base_revision,
                payload_sha256,
                _json(operation.payload),
                receipt.status.value,
                _json(receipt.as_dict()),
                now,
                now,
            ),
        )
        return cursor.rowcount == 1

    def acknowledge_cursor(
        self, device_id: str, projection_version: int, sequence: int
    ) -> None:
        if sequence < 0:
            raise ValueError("cursor sequence cannot be negative")
        self.connection.execute(
            """
            INSERT INTO sync_cursors (
                device_id, projection_version, acknowledged_sequence, updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                projection_version = excluded.projection_version,
                acknowledged_sequence = MAX(
                    sync_cursors.acknowledged_sequence,
                    excluded.acknowledged_sequence
                ),
                updated_at = excluded.updated_at
            """,
            (device_id, projection_version, sequence, self.now()),
        )


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
        revision=int(row["revision"]),
    )


def _artifact(row: sqlite3.Row) -> Artifact:
    input_refs = json.loads(str(row["input_refs_json"]))
    metadata = json.loads(str(row["metadata_json"]))
    if not isinstance(input_refs, list) or not isinstance(metadata, dict):
        raise ValueError("stored artifact JSON is invalid")
    return Artifact(
        artifact_id=str(row["artifact_id"]),
        run_id=row["run_id"],
        kind=str(row["kind"]),
        producer=str(row["producer"]),
        producer_version=str(row["producer_version"]),
        config_digest=str(row["config_digest"]),
        input_refs=tuple(str(value) for value in input_refs),
        storage_ref=str(row["storage_ref"]),
        sha256=row["sha256"],
        size_bytes=(int(row["size_bytes"]) if row["size_bytes"] is not None else None),
        status=str(row["status"]),
        metadata=metadata,
        legacy_ref=row["legacy_ref"],
        created_at=_parse_datetime(row["created_at"]),
    )


def _change_event(row: sqlite3.Row) -> ChangeEvent:
    payload = row["payload_json"]
    return ChangeEvent(
        sequence=int(row["sequence"]),
        resource_type=str(row["resource_type"]),
        resource_id=str(row["resource_id"]),
        revision=int(row["revision"]),
        operation=ChangeOperation(str(row["operation"])),
        payload=json.loads(str(payload)) if payload is not None else None,
        created_at=_parse_datetime(row["created_at"]),
    )


def _device_credential(row: sqlite3.Row) -> DeviceCredential:
    scopes = json.loads(str(row["scopes_json"]))
    if not isinstance(scopes, list):
        raise ValueError("stored device scopes are invalid")
    return DeviceCredential(
        credential_id=str(row["credential_id"]),
        device_id=str(row["device_id"]),
        key_id=str(row["key_id"]),
        algorithm=str(row["algorithm"]),
        public_key=str(row["public_key"]),
        scopes=tuple(DeviceScope(str(value)) for value in scopes),
        passkey_credential_ref=str(row["passkey_credential_ref"]),
        created_at=_parse_datetime(row["created_at"]),
        last_used_at=_optional_parse_datetime(row["last_used_at"]),
        revoked_at=_optional_parse_datetime(row["revoked_at"]),
    )


def _client_operation_record(row: sqlite3.Row) -> ClientOperationRecord:
    payload = json.loads(str(row["payload_json"]))
    receipt = json.loads(str(row["receipt_json"]))
    if not isinstance(payload, dict) or not isinstance(receipt, dict):
        raise ValueError("stored client operation JSON is invalid")
    return ClientOperationRecord(
        device_id=str(row["device_id"]),
        operation=ClientOperation(
            operation_id=str(row["operation_id"]),
            kind=str(row["kind"]),
            base_revision=(
                int(row["base_revision"]) if row["base_revision"] is not None else None
            ),
            payload=payload,
        ),
        payload_sha256=str(row["payload_sha256"]),
        receipt=OperationReceipt(
            operation_id=str(receipt["operation_id"]),
            status=ClientOperationStatus(str(receipt["status"])),
            resource_revision=(
                int(receipt["resource_revision"])
                if receipt.get("resource_revision") is not None
                else None
            ),
            error=receipt.get("error"),
        ),
        created_at=_parse_datetime(row["created_at"]),
        completed_at=_parse_datetime(row["completed_at"]),
    )


def _datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("database timestamps must be timezone-aware")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


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


def _json_object(value: object) -> dict[str, Any]:
    decoded = json.loads(str(value))
    if not isinstance(decoded, dict):
        raise ValueError("stored JSON value is not an object")
    return decoded


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "SqliteArtifactRepository",
    "SqliteAuditRepository",
    "SqliteChangeLogRepository",
    "SqliteCorrectionRepository",
    "SqliteDeviceRepository",
    "SqliteDeviceTrustRepository",
    "SqliteIdempotencyRepository",
    "SqliteMobileSyncRepository",
    "SqliteProcessingRunRepository",
    "SqliteRecordingCatalogRepository",
    "SqliteTombstoneRepository",
    "utc_now",
]
