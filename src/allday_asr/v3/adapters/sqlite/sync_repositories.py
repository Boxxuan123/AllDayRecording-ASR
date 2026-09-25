from __future__ import annotations
import sqlite3
from allday_asr.v3.domain.device_sync import (
    ClientOperation,
    ClientOperationRecord,
    DeviceCredential,
    DeviceScope,
    OperationReceipt,
    PairingRecord,
)

from .core_repository_codec import (
    _device_credential,
    _client_operation_record,
    _datetime,
    _optional_datetime,
    _json,
)
from .repository_clock import Clock


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

    def recover_annotation_receipt(self, record):
        """Replay a legacy accepted request without reapplying its payload.

        Publish current authoritative rows as a recovery barrier per resource.
        These targets may be newer than the original operation, never guessed
        from its base or group maximum. Receipt enrichment is persisted atomically.
        """
        from dataclasses import replace
        from .evidence_projection_repository import SqliteEvidenceProjectionRepository
        from .operational_repositories import SqliteChangeLogRepository
        from allday_asr.v3.contracts import utterance_dto
        import json
        cached = self.connection.execute('SELECT resource_results_json FROM annotation_receipt_recovery WHERE operation_id=?', (record.operation.operation_id,)).fetchone()
        if cached:
            return replace(record.receipt, resource_results=tuple(json.loads(cached[0])))
        evidence = SqliteEvidenceProjectionRepository(self.connection, now=self.now)
        changes = SqliteChangeLogRepository(self.connection, now=self.now)
        results = []
        for selection in record.operation.payload.get('selections', []):
            uid = selection['utterance_id']
            try:
                row = evidence.get_utterance(uid)
            except KeyError:
                revision = self.connection.execute(
                    "SELECT COALESCE(MAX(revision),0)+1 FROM change_events WHERE resource_type='utterance' AND resource_id=?", (uid,)
                ).fetchone()[0]
                changes.append('utterance', uid, revision, 'tombstone', None)
            else:
                revision = row.revision
                changes.append('utterance', uid, revision, 'upsert', utterance_dto(row,
                    speaker_label=evidence.speaker_label(row.speaker_track_id),
                    original_speaker_label=evidence.speaker_label(row.original_speaker_track_id)))
            results.append({'resource_id': uid, 'revision': revision})
        receipt = replace(record.receipt, resource_results=tuple(results))
        self.connection.execute('INSERT INTO annotation_receipt_recovery VALUES(?,?)',
                                (record.operation.operation_id, _json(results)))
        return receipt

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
