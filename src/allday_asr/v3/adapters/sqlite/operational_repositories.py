from __future__ import annotations
import json
import sqlite3
from typing import Any
from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.models import (
    ChangeEvent,
    CorrectionOperation,
)

from .core_repository_codec import (
    _change_event,
    _datetime,
    _parse_datetime,
    _json,
    _json_object,
)
from .repository_clock import Clock


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
        if resource_type == "recording_session" and operation == "upsert" and payload is not None:
            manifest = self.connection.execute(
                "SELECT json_extract(entries_json, '$.sessionKey') AS session_key "
                "FROM session_manifests WHERE session_id = ?",
                (resource_id,),
            ).fetchone()
            if manifest is not None and isinstance(manifest["session_key"], str):
                payload = {**payload, "session_key": manifest["session_key"]}
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
