from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import logging
from contextlib import contextmanager
from uuid import uuid4
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AutomaticWorkflowStateStore:
    """Cross-process state and retry requests for receiver-owned workflows."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.requests_root = self.root / "retry-requests"

    @contextmanager
    def _transaction(self):
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.root / "workflow-state.sqlite3", timeout=30)
        try:
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            if version > 3:
                raise RuntimeError('workflow sidecar schema is newer than this application')
            connection.execute("CREATE TABLE IF NOT EXISTS workflow_states (session_id TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS retry_requests (request_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, requested_at TEXT NOT NULL, status TEXT NOT NULL)")
            if version < 3:
                columns = {r[1] for r in connection.execute('PRAGMA table_info(retry_requests)')}
                for column in ('origin', 'scheduled_state_json'):
                    if column not in columns:
                        connection.execute(f'ALTER TABLE retry_requests ADD COLUMN {column} TEXT')
                connection.execute('CREATE TABLE IF NOT EXISTS legacy_imports (source_key TEXT PRIMARY KEY, source_json TEXT NOT NULL, imported_at TEXT NOT NULL)')
                connection.execute('CREATE TABLE IF NOT EXISTS retry_request_audit (request_id TEXT PRIMARY KEY, action TEXT NOT NULL, actor TEXT NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL)')
                connection.execute('PRAGMA user_version=3')
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        # Each original file is interpreted once; current SQLite state is never legacy input.
        self.requests_root.mkdir(parents=True, exist_ok=True)
        with self._transaction() as connection:
            for path in self.root.glob("*.json"):
                source_key = 'state:' + path.stem
                if connection.execute('SELECT 1 FROM legacy_imports WHERE source_key=?', (source_key,)).fetchone():
                    continue
                value = self._read(path)
                if value is None:
                    continue
                sid = value["session_id"]
                connection.execute("INSERT OR IGNORE INTO workflow_states VALUES (?,?)", (sid, json.dumps(value)))
                if value.get("status") == "retry_requested" and not self._request_path(sid).is_file():
                    requested = value.get("retry_requested_at") or value.get("updated_at") or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
                    rid = "legacy:" + _key(sid + requested)
                    connection.execute("INSERT OR IGNORE INTO retry_requests(request_id,session_id,requested_at,status,origin) VALUES (?,?,?,'pending','legacy-state')", (rid, sid, requested))
                self._mark_import(connection, source_key, value)
            for path in self.requests_root.glob('*.json'):
                source_key = 'request:' + path.stem
                if connection.execute('SELECT 1 FROM legacy_imports WHERE source_key=?', (source_key,)).fetchone():
                    continue
                value = self._read(path)
                sid = value['session_id']
                requested = value.get('requested_at') or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
                rid = 'legacy:' + _key(sid + requested)
                connection.execute("INSERT OR IGNORE INTO workflow_states VALUES (?,?)", (sid, json.dumps({
                    'session_id': sid, 'status': 'retry_requested', 'retry_requested_at': requested, 'updated_at': requested})))
                connection.execute("INSERT OR IGNORE INTO retry_requests(request_id,session_id,requested_at,status,origin) VALUES (?,?,?,'pending','legacy-file')", (rid, sid, requested))
                self._mark_import(connection, source_key, value)

    @staticmethod
    def _mark_import(connection, source_key, value):
        connection.execute('INSERT INTO legacy_imports VALUES (?,?,?)', (source_key, json.dumps(value), _utc_now()))

    def status(self, session_id: str) -> dict[str, Any] | None:
        with self._transaction() as connection:
            row = connection.execute("SELECT value FROM workflow_states WHERE session_id=?", (session_id,)).fetchone()
        return json.loads(row[0]) if row else self._read(self._status_path(session_id), session_id=session_id)

    def statuses(self) -> tuple[dict[str, Any], ...]:
        self.initialize()
        with self._transaction() as connection:
            values = [json.loads(row[0]) for row in connection.execute("SELECT value FROM workflow_states")]
        return tuple(sorted(values, key=lambda v: (v.get("updated_at", ""), v["session_id"]), reverse=True))

    def save(self, session_id: str, value: Mapping[str, Any], *, claim_request_id=None) -> bool:
        if str(value.get("session_id") or session_id) != session_id:
            raise ValueError("automatic workflow state has a mismatched session id")
        with self._transaction() as connection:
            row = connection.execute("SELECT value FROM workflow_states WHERE session_id=?", (session_id,)).fetchone()
            previous = json.loads(row[0]) if row else {}
            if claim_request_id:
                if previous.get('status') in {'queued', 'running'}:
                    return False
                claim = connection.execute("UPDATE retry_requests SET status='scheduled' WHERE request_id=? AND session_id=? AND status='pending'",
                                           (claim_request_id, session_id))
                if claim.rowcount != 1:
                    return False
            merged = {**value, "session_id": session_id, "version": 3}
            if claim_request_id:
                merged['manual_retry_count'] = int(previous.get('manual_retry_count') or 0) + 1
            for name in ("manual_retry_count", "target_input_revision"):
                if name in previous or name in merged:
                    merged[name] = max(int(previous.get(name) or 0), int(merged.get(name) or 0))
            connection.execute("INSERT OR REPLACE INTO workflow_states VALUES (?,?)", (session_id, json.dumps(merged, ensure_ascii=False)))
            rid = value.get("retry_request_id")
            if rid and value.get("status") == "queued":
                # Durable scheduling and acknowledgement are one transaction.
                connection.execute("UPDATE retry_requests SET status='scheduled',scheduled_state_json=? WHERE request_id=? AND session_id=? AND status IN ('pending','scheduled')", (json.dumps(merged), rid, session_id))
            return True

    def request_retry(self, session_id: str) -> dict[str, Any]:
        self.initialize()
        with self._transaction() as connection:
            row = connection.execute("SELECT value FROM workflow_states WHERE session_id=?", (session_id,)).fetchone()
            if row is None:
                raise KeyError(f"automatic workflow does not exist: {session_id}")
            current = json.loads(row[0])
            pending = connection.execute("SELECT request_id,requested_at FROM retry_requests WHERE session_id=? AND status='pending'", (session_id,)).fetchone()
            if pending:
                rid, requested_at = pending
            else:
                if current.get("status") != "needs_attention":
                    raise ValueError("only an automatic workflow needing attention can be retried")
                rid, requested_at = uuid4().hex, _utc_now()
                connection.execute("INSERT INTO retry_requests(request_id,session_id,requested_at,status,origin) VALUES (?,?,?,'pending','manual')", (rid, session_id, requested_at))
            current.update(status="retry_requested", retry_requested_at=requested_at, updated_at=requested_at,
                           retry_request_id=rid,
                           detail="已请求重试，等待接收服务领取")
            connection.execute("UPDATE workflow_states SET value=? WHERE session_id=?", (json.dumps(current, ensure_ascii=False), session_id))
        return {"session_id": session_id, "status": "retry_requested", "requested_at": requested_at, "request_id": rid}

    def consume_retry_requests(self) -> tuple[dict[str, Any], ...]:
        # Compatibility name: a replayable read, never destructive consume.
        self.initialize()
        with self._transaction() as connection:
            return tuple({"request_id": r[0], "session_id": r[1], "requested_at": r[2], "version": 2}
                         for r in connection.execute("SELECT request_id,session_id,requested_at FROM retry_requests WHERE status='pending' ORDER BY requested_at,request_id"))

    def retry_request_report(self, session_id):
        with self._transaction() as connection:
            return self._retry_request_report(connection, session_id)

    @staticmethod
    def _retry_request_report(connection, session_id):
        state = connection.execute('SELECT value FROM workflow_states WHERE session_id=?', (session_id,)).fetchone()
        current = json.loads(state[0]) if state else {}
        rows = [dict(zip(('request_id', 'requested_at', 'status', 'origin', 'scheduled_state'), row, strict=True))
                for row in connection.execute('SELECT request_id,requested_at,status,origin,scheduled_state_json FROM retry_requests WHERE session_id=? ORDER BY requested_at,request_id', (session_id,))]
        for row in rows:
            row['scheduled_state'] = json.loads(row['scheduled_state']) if row['scheduled_state'] else None
        imported = [json.loads(r[0]) for r in connection.execute(
            "SELECT source_json FROM legacy_imports WHERE source_key LIKE 'state:%' AND json_extract(source_json,'$.session_id')=?", (session_id,))]
        old_intent = connection.execute("SELECT 1 FROM legacy_imports WHERE source_key LIKE 'request:%' AND json_extract(source_json,'$.session_id')=?", (session_id,)).fetchone()
        no_legacy_intent = bool(imported and all(v.get('status') != 'retry_requested' for v in imported) and not old_intent)
        pairs = []
        for manual in rows:
            if manual['request_id'].startswith('legacy:'):
                continue
            for legacy in rows:
                if legacy['request_id'] != 'legacy:' + _key(session_id + manual['requested_at']):
                    continue
                proof = (no_legacy_intent and legacy['origin'] is None and
                         current.get('retry_requested_at') == manual['requested_at'] and
                         current.get('retry_request_id') in {None, manual['request_id']})
                can_merge = bool(proof and current.get('status') == 'retry_requested' and
                                 manual['status'] == legacy['status'] == 'pending')
                pairs.append({'manual_request_id': manual['request_id'], 'legacy_request_id': legacy['request_id'],
                    'can_merge': can_merge, 'proof': 'legacy_id_derived_from_new_request_without_legacy_intent' if proof else None,
                    'reason': 'unclaimed_duplicate' if can_merge else 'requires_review_or_already_scheduled'})
        return {'session_id': session_id, 'workflow_state': current, 'requests': rows, 'duplicate_pairs': pairs}

    def coalesce_legacy_duplicate(self, session_id, legacy_request_id, manual_request_id, actor):
        if not actor.strip():
            raise ValueError('retry repair requires an actor')
        with self._transaction() as connection:
            old = connection.execute('SELECT details_json FROM retry_request_audit WHERE request_id=?', (legacy_request_id,)).fetchone()
            if old and json.loads(old[0]).get('manual_request_id') == manual_request_id:
                return {'merged': False, 'already_resolved': True}
            report = self._retry_request_report(connection, session_id)
            pair = next((p for p in report['duplicate_pairs'] if p['legacy_request_id'] == legacy_request_id
                         and p['manual_request_id'] == manual_request_id and p['can_merge']), None)
            if pair is None:
                raise ValueError('duplicate retry cannot be proved unclaimed; review required')
            connection.execute("UPDATE retry_requests SET status='superseded' WHERE request_id=? AND status='pending'", (legacy_request_id,))
            connection.execute('INSERT INTO retry_request_audit VALUES (?,?,?,?,?)',
                (legacy_request_id, 'superseded_legacy_duplicate', actor, json.dumps({**pair, 'session_id': session_id}), _utc_now()))
            return {'merged': True, 'already_resolved': False}

    def _status_path(self, session_id: str) -> Path:
        return self.root / f"{_key(session_id)}.json"

    def _request_path(self, session_id: str) -> Path:
        return self.requests_root / f"{_key(session_id)}.json"

    @staticmethod
    def _read(
        path: Path, *, session_id: str | None = None
    ) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logging.getLogger(__name__).error("workflow state read failed kind=%s key=%s", type(exc).__name__, path.stem)
            raise ValueError("workflow state is corrupt or unavailable") from exc
        if not isinstance(value, dict) or not isinstance(value.get("session_id"), str):
            raise ValueError("workflow state has an invalid structure")
        if session_id is not None and value.get("session_id") != session_id:
            raise ValueError("workflow state identity mismatch")
        return value

    @staticmethod
    def _write(path: Path, value: Mapping[str, Any]) -> None:
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str),
            encoding="utf-8",
        )
        os.replace(temporary, path)


def _key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = ["AutomaticWorkflowStateStore"]
