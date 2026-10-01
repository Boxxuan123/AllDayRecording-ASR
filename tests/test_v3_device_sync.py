from __future__ import annotations

import base64
import hashlib
import json
import queue
import shutil
import threading
import unittest
from unittest.mock import MagicMock, patch
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from allday_asr.v3.interfaces.transfer.devices import (
    DEVICE_ALGORITHM,
    DeviceAuthManager,
    DeviceCredentialStore,
    DeviceForbiddenError,
    DeviceUnauthorizedError,
    build_device_signature_payload,
)
from allday_asr.v3.interfaces.transfer.passkeys import RequestBinding
from allday_asr.v3.interfaces.transfer.server import create_transfer_server
from allday_asr.v3.interfaces.transfer.store import UploadConflictError, UploadRecord, UploadStore, UploadStoreError
from allday_asr.v3.adapters.files import ContentAddressedStore
from allday_asr.v3.adapters.backup import FilesystemSessionBackupAdapter
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork, V3Database
from allday_asr.v3.adapters.transfer import (
    AutomaticWorkflowStateStore,
    TransferDeviceTrustAdapter,
    V3AutomaticWorkflowRunner,
    V3UploadIngestAdapter,
)
from allday_asr.v3.adapters.transfer.ingest import _parse_manifest
from allday_asr.v3.adapters.transfer.automation import SupersededInput
from allday_asr.v3.application import MobileSyncService
from allday_asr.v3.domain import (
    ChangeOperation,
    ClientOperation,
    ClientOperationStatus,
    OperationReceipt,
    SyncRequest,
    stable_ulid,
)
from allday_asr.v3.interfaces.device_gateway import DeviceGateway
from allday_asr.v3 import PROJECTION_VERSION


RECEIVER_ID = "a" * 64


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


@contextmanager
def _workspace_directory():
    path = Path(__file__).parent / f"v3-sync-{uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class _RecordingOperationHandler:
    def __init__(self) -> None:
        self.calls = 0

    def apply(
        self, operation: ClientOperation, uow: SqliteUnitOfWork
    ) -> OperationReceipt:
        self.calls += 1
        resource_id = stable_ulid("phone-operation", operation.operation_id)
        uow.changes.append(
            "review_item",
            resource_id,
            2,
            ChangeOperation.UPSERT.value,
            {
                "review_item_id": resource_id,
                "revision": 2,
                "decision": operation.payload["decision"],
            },
        )
        return OperationReceipt(
            operation_id=operation.operation_id,
            status=ClientOperationStatus.APPLIED,
            resource_revision=2,
            error=None,
        )


class _ReceiptMatrixOperationHandler:
    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    def apply(
        self, operation: ClientOperation, uow: SqliteUnitOfWork
    ) -> OperationReceipt:
        self.calls[operation.kind] = self.calls.get(operation.kind, 0) + 1
        if operation.kind in {"review.apply", "review.crash"}:
            resource_id = stable_ulid("receipt-matrix", operation.operation_id)
            uow.changes.append(
                "review_item",
                resource_id,
                2,
                ChangeOperation.UPSERT.value,
                {"review_item_id": resource_id, "revision": 2},
            )
            if operation.kind == "review.crash":
                raise RuntimeError("injected handler crash")
            return OperationReceipt(
                operation_id=operation.operation_id,
                status=ClientOperationStatus.APPLIED,
                resource_revision=2,
                error=None,
            )
        status = (
            ClientOperationStatus.CONFLICT
            if operation.kind == "review.conflict"
            else ClientOperationStatus.REJECTED
        )
        code = "REVISION_CONFLICT" if status is ClientOperationStatus.CONFLICT else "INVALID"
        return OperationReceipt(
            operation_id=operation.operation_id,
            status=status,
            resource_revision=None,
            error={
                "code": code,
                "message": f"terminal {status.value}",
                "details": {"payload": operation.payload},
                "request_id": stable_ulid("receipt-error", operation.operation_id),
            },
        )


class V3DeviceSyncTests(unittest.TestCase):
    def test_automatic_workflow_retry_request_is_cross_process_durable(self) -> None:
        with _workspace_directory() as root:
            store = AutomaticWorkflowStateStore(root / "automation")
            store.save(
                "session-retry",
                {
                    "version": 1,
                    "session_id": "session-retry",
                    "status": "needs_attention",
                    "stage": "backup",
                    "detail": "backup failed",
                    "created_at": "2026-09-02T00:00:00Z",
                    "updated_at": "2026-09-02T00:01:00Z",
                },
            )

            accepted = store.request_retry("session-retry")

            self.assertEqual(accepted["status"], "retry_requested")
            self.assertEqual(store.status("session-retry")["status"], "retry_requested")
            requests = store.consume_retry_requests()
            self.assertEqual([value["session_id"] for value in requests], ["session-retry"])
            self.assertEqual(store.consume_retry_requests(), ())

    def test_automatic_workflow_exhaustion_moves_to_attention_inbox(self) -> None:
        state: dict[str, object] = {
            "session_id": "session-failure",
            "status": "running",
            "stage": "backup",
            "auto_retry_count": 0,
        }
        runner = object.__new__(V3AutomaticWorkflowRunner)
        runner.max_auto_retries = 1
        runner.retry_delays = (0.0,)
        runner.status = lambda selected: dict(state)

        def progress(selected, status, stage, detail, **values):
            state.update(status=status, stage=stage, detail=detail, **values)

        runner._progress = progress

        runner._handle_failure("session-failure", RuntimeError("temporary"))
        self.assertEqual(state["status"], "retry_scheduled")
        self.assertEqual(state["auto_retry_count"], 1)

        runner._handle_failure("session-failure", RuntimeError("still broken"))
        self.assertEqual(state["status"], "needs_attention")
        self.assertTrue(state["needs_manual_retry"])

    def test_automation_retry_preserves_completed_processing_job(self) -> None:
        session_id = "session-existing"
        written: dict[str, object] = {}
        runner = object.__new__(V3AutomaticWorkflowRunner)
        runner._lock = threading.RLock()
        runner._closed = False
        runner._active = set()
        runner._queue = SimpleNamespace(put=lambda selected: None)
        runner._input_revision = lambda selected: 1
        runner.status = lambda selected: {
            "session_id": selected,
            "status": "failed",
            "stage": "speaker_identity",
            "job_id": "job-existing",
        }
        runner._write = lambda selected, value: written.update(value)

        runner.submit(
            SimpleNamespace(upload_id="upload-retry"),
            {"session_id": session_id},
        )

        self.assertEqual(written["status"], "queued")
        self.assertEqual(written["job_id"], "job-existing")

    def test_inflight_append_persists_target_and_queues_latest_once(self) -> None:
        session_id = "session-append"
        state = {"version": 1, "session_id": session_id, "status": "running",
                 "stage": "processing", "input_revision": 1,
                 "target_input_revision": 1, "attempt_count": 1}
        runner = object.__new__(V3AutomaticWorkflowRunner)
        runner._lock = threading.RLock()
        runner._closed = False
        runner.poll_interval = 0.01
        runner.max_auto_retries = 3
        runner._active = {session_id}
        runner._queue = queue.Queue()
        runner._input_revision = lambda _selected: 2
        runner.status = lambda _selected: dict(state)

        def write(_selected, value):
            state.clear()
            state.update(value)

        runner._write = write
        runner._poll_persistent_work = lambda: None

        def superseded(_selected):
            raise SupersededInput(session_id)

        runner._process = superseded
        for _ in range(2):
            result = runner.submit(SimpleNamespace(upload_id="upload-2"),
                                   {"session_id": session_id})
            self.assertEqual(result["target_input_revision"], 2)
        self.assertEqual(runner._queue.qsize(), 0)
        with self.assertRaises(SupersededInput):
            runner._require_input_revision(session_id, 1)
        runner._queue.put(session_id)
        runner._queue.put(None)
        runner._run()
        self.assertEqual(state["status"], "queued")
        self.assertEqual(state["input_revision"], 2)
        self.assertEqual(runner._queue.get_nowait(), session_id)

    def test_restart_recovers_newer_target_after_old_run_completed(self) -> None:
        session_id = "session-restart-append"
        with _workspace_directory() as root:
            AutomaticWorkflowStateStore(root / "automation").save(session_id, {
                "session_id": session_id, "status": "completed",
                "input_revision": 1, "target_input_revision": 2,
            })
            runner = object.__new__(V3AutomaticWorkflowRunner)
            runner._states = AutomaticWorkflowStateStore(root / "automation")
            runner._input_revision = lambda _selected: 2
            database = MagicMock()
            database.read.return_value.__enter__.return_value.execute.return_value.fetchall.return_value = []
            runner.core = SimpleNamespace(database=database)
            queued = []
            runner._queue_session = lambda selected, **values: queued.append((selected, values))
            runner._recover_pending()
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0][1]["input_revision"], 2)

    def test_automation_reuses_completed_processing_when_postprocessing_retries(
        self,
    ) -> None:
        session_id = "session-existing"
        snapshot = SimpleNamespace(
            job=SimpleNamespace(job_id="job-existing", status="succeeded"),
            run=SimpleNamespace(
                session_id=session_id,
                pipeline_version="v3-native.1",
                input_revision=1,
            ),
        )
        runner = object.__new__(V3AutomaticWorkflowRunner)
        runner.core = SimpleNamespace(
            processing=SimpleNamespace(get=lambda job_id: snapshot)
        )
        runner.pipeline_version = "v3-native.1"
        runner._input_revision = lambda selected: 1
        runner.status = lambda selected: {
            "session_id": selected,
            "status": "failed",
            "stage": "speaker_identity",
            "job_id": "job-existing",
        }

        reused = runner._completed_processing_snapshot(session_id)

        self.assertIs(reused, snapshot)
        self.assertIsNone(runner._completed_processing_snapshot("other-session"))

    def _environment(self, root: Path):
        database = V3Database.open(root / "core.sqlite3")
        private_key = ec.generate_private_key(ec.SECP256R1())
        public_key = private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        legacy = DeviceAuthManager(DeviceCredentialStore(root / "devices.json"))
        record = legacy.store.register(
            device_name="Harmony phone",
            algorithm=DEVICE_ALGORITHM,
            public_key=_b64(public_key),
            passkey_credential_id="passkey-id",
        )
        trust = TransferDeviceTrustAdapter(
            legacy,
            lambda: SqliteUnitOfWork(database),
            RECEIVER_ID,
        )
        self.assertTrue(trust.enroll(record))
        handler = _RecordingOperationHandler()
        service = MobileSyncService(
            lambda: SqliteUnitOfWork(database), operation_handler=handler
        )
        return database, private_key, record, trust, handler, service

    def test_signed_batch_is_idempotent_and_pulls_changes(self) -> None:
        with _workspace_directory() as root:
            database, _, record, trust, handler, service = self._environment(root)
            operation = ClientOperation(
                operation_id=stable_ulid("operation", "one"),
                kind="review.resolve",
                base_revision=1,
                payload={"decision": "accepted"},
            )
            request = SyncRequest(
                projection_version=PROJECTION_VERSION,
                cursor=None,
                client_operations=(operation,),
                pull_limit=100,
            )
            device_id = trust.domain_device_id(record.device_id)

            first = service.synchronize(device_id, request)
            replay = service.synchronize(device_id, request)

            self.assertEqual(handler.calls, 1)

            self.assertEqual(first.receipts, replay.receipts)
            self.assertEqual(first.receipts[0].status, ClientOperationStatus.APPLIED)
            self.assertEqual(len(first.changes), 1)
            self.assertEqual(first.next_cursor, "cursor-1")
            with database.read() as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM client_operations"
                ).fetchone()[0]
            self.assertEqual(count, 1)

            reused = service.synchronize(
                device_id,
                SyncRequest(
                    projection_version=PROJECTION_VERSION,
                    cursor=first.next_cursor,
                    client_operations=(
                        ClientOperation(
                            operation_id=operation.operation_id,
                            kind=operation.kind,
                            base_revision=operation.base_revision,
                            payload={"decision": "rejected"},
                        ),
                    ),
                    pull_limit=100,
                ),
            )
            self.assertEqual(
                reused.receipts[0].status, ClientOperationStatus.CONFLICT
            )
            self.assertEqual(handler.calls, 1)

    def test_utterance_tombstone_with_audit_payload_syncs_as_null(self) -> None:
        with _workspace_directory() as root:
            database, _, record, trust, _, service = self._environment(root)
            utterance_id = stable_ulid("retired-utterance", "one")
            with SqliteUnitOfWork(database) as uow:
                uow.changes.append(
                    "utterance", utterance_id, 2,
                    ChangeOperation.TOMBSTONE.value,
                    {"utterance_id": utterance_id, "replacement_run_id": "new-run"},
                )
            response = service.synchronize(
                trust.domain_device_id(record.device_id),
                SyncRequest(PROJECTION_VERSION, None, (), 100),
            )
            self.assertEqual(len(response.changes), 1)
            self.assertEqual(response.changes[0].operation, "tombstone")
            self.assertIsNone(response.changes[0].resource)

    def test_terminal_receipts_replay_and_crash_rollback_are_atomic(self) -> None:
        with _workspace_directory() as root:
            database, _, record, trust, _, _ = self._environment(root)
            handler = _ReceiptMatrixOperationHandler()
            service = MobileSyncService(
                lambda: SqliteUnitOfWork(database), operation_handler=handler
            )
            operations = tuple(
                ClientOperation(
                    operation_id=stable_ulid("receipt-matrix", kind),
                    kind=kind,
                    base_revision=1,
                    payload={"decision": kind.rsplit(".", 1)[-1]},
                )
                for kind in ("review.apply", "review.conflict", "review.reject")
            )
            request = SyncRequest(PROJECTION_VERSION, None, operations, 100)
            device_id = trust.domain_device_id(record.device_id)

            first = service.synchronize(device_id, request)
            replay = service.synchronize(device_id, request)

            self.assertEqual(
                [receipt.status for receipt in first.receipts],
                [
                    ClientOperationStatus.APPLIED,
                    ClientOperationStatus.CONFLICT,
                    ClientOperationStatus.REJECTED,
                ],
            )
            self.assertEqual(first.receipts, replay.receipts)
            self.assertEqual(handler.calls, {
                "review.apply": 1,
                "review.conflict": 1,
                "review.reject": 1,
            })
            with database.read() as connection:
                rows = connection.execute(
                    "SELECT kind, payload_json, status, receipt_json "
                    "FROM client_operations ORDER BY kind"
                ).fetchall()
                change_count = int(
                    connection.execute("SELECT COUNT(*) FROM change_events").fetchone()[0]
                )
            self.assertEqual(len(rows), 3)
            self.assertEqual({str(row["status"]) for row in rows}, {
                "applied", "conflict", "rejected"
            })
            rejected = next(row for row in rows if row["status"] == "rejected")
            self.assertEqual(
                json.loads(str(rejected["payload_json"])), {"decision": "reject"}
            )
            self.assertEqual(
                json.loads(str(rejected["receipt_json"])),
                first.receipts[2].as_dict(),
            )
            self.assertEqual(change_count, 1)

            crashing = ClientOperation(
                operation_id=stable_ulid("receipt-matrix", "crash"),
                kind="review.crash",
                base_revision=1,
                payload={"decision": "crash"},
            )
            with self.assertRaisesRegex(RuntimeError, "injected handler crash"):
                service.synchronize(
                    device_id,
                    SyncRequest(
                        PROJECTION_VERSION, first.next_cursor, (crashing,), 100
                    ),
                )
            with database.read() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM client_operations"
                    ).fetchone()[0],
                    3,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM change_events").fetchone()[0],
                    1,
                )

    def test_revoke_blocks_new_challenges(self) -> None:
        with _workspace_directory() as root:
            _, _, record, trust, _, _ = self._environment(root)
            self.assertTrue(
                trust.revoke(record.device_id, actor="desktop-admin", reason="lost")
            )
            with self.assertRaises(DeviceUnauthorizedError):
                trust.start_authentication(
                    device_id=record.device_id,
                    binding=RequestBinding.for_request(
                        method="GET", path="/device/v3/status", body=b""
                    ),
                )

    def test_scope_restriction_blocks_sync_before_challenge_issue(self) -> None:
        with _workspace_directory() as root:
            database, _, record, trust, _, _ = self._environment(root)
            with database.transaction() as connection:
                connection.execute(
                    "UPDATE device_credentials SET scopes_json = ? WHERE key_id = ?",
                    ('["device.status"]', record.device_id),
                )
            with self.assertRaises(DeviceForbiddenError):
                trust.start_authentication(
                    device_id=record.device_id,
                    binding=RequestBinding.for_request(
                        method="POST", path="/device/v3/sync", body=b"{}"
                    ),
                )

    def test_manifest_continuity_is_recomputed_from_chunk_timeline(self) -> None:
        manifest = {
            "format": "AllDayRecording session manifest v2",
            "sessionKey": "watch-session:1767225600000",
            "sessionStartedAt": 1767225600000,
            "device": "HUAWEI WATCH 5",
            "timezone": "Asia/Singapore",
            "audio": {
                "sampleRate": 16000,
                "channels": 1,
                "bitsPerSample": 16,
            },
            "chunks": [
                {
                    "index": 21,
                    "fileName": "021_segment.wav",
                    "firstSample": 0,
                    "sampleCount": 100,
                },
                {
                    "index": 30,
                    "fileName": "030_segment.wav",
                    "firstSample": 100,
                    "sampleCount": 100,
                },
            ],
            "completedSegments": 2,
            "totalSamples": 200,
            "continuityValid": True,
            "completion": {"source": "watch_stop", "completedSegments": 2,
                           "totalSamples": 200, "confirmedAt": 1767225601000},
        }

        complete = dict(manifest)
        complete["chunks"] = [manifest["chunks"][0], {**manifest["chunks"][1], "index": 22}]
        self.assertTrue(_parse_manifest(complete)["continuityValid"])
        missing_completion = dict(complete)
        del missing_completion["completion"]
        with self.assertRaisesRegex(UploadStoreError, "字段不符合协议"):
            _parse_manifest(missing_completion)
        wrong_count = dict(complete)
        wrong_count["completion"] = {**complete["completion"], "completedSegments": 1}
        with self.assertRaisesRegex(UploadStoreError, "结束证明与最终清单不一致"):
            _parse_manifest(wrong_count)
        with self.assertRaises(UploadStoreError):
            _parse_manifest(manifest)

    def test_completed_manifest_admits_audio_and_is_idempotent(self) -> None:
        with _workspace_directory() as root:
            database, _, record, trust, _, _ = self._environment(root)
            store = UploadStore(root / "inbox")
            audio_store = ContentAddressedStore(root / "audio")
            artifact_store = ContentAddressedStore(root / "artifacts")
            directory = "pcm_session_1767225600000"
            chunks = []
            completed_recordings = []
            for index in range(2):
                name = f"segment_{index}_first_{index * 100}.wav"
                content = bytes([index + 1]) * 244
                upload, _ = store.create_upload(
                    relative_path=f"{directory}/{name}",
                    size=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    kind="recording",
                )
                completed = store.append_chunk(upload.upload_id, offset=0, data=content)
                self.assertEqual(completed.status, "completed")
                completed_recordings.append(completed)
                chunks.append(
                    {
                        "index": index,
                        "fileName": name,
                        "firstSample": index * 100,
                        "sampleCount": 100,
                    }
                )
            manifest = {
                "format": "AllDayRecording session manifest v2",
                "sessionKey": "watch-session:1767225600000",
                "sessionStartedAt": 1767225600000,
                "device": "HUAWEI WATCH 5",
                "timezone": "Asia/Singapore",
                "audio": {
                    "sampleRate": 16000,
                    "channels": 1,
                    "bitsPerSample": 16,
                },
                "chunks": chunks,
                "completedSegments": 2,
                "totalSamples": 200,
                "continuityValid": True,
                "completion": {"source": "watch_stop", "completedSegments": 2,
                               "totalSamples": 200, "confirmedAt": 1767225601000},
            }
            manifest_bytes = json.dumps(manifest).encode()
            upload, _ = store.create_upload(
                relative_path=f"{directory}/session_summary.json",
                size=len(manifest_bytes),
                sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                kind="manifest",
            )
            manifest_record = store.append_chunk(
                upload.upload_id, offset=0, data=manifest_bytes
            )
            existing_asset_id = stable_ulid("historical-import-asset", "first")
            existing_media = audio_store.put_file(
                store.completed_path(completed_recordings[0]),
                expected_sha256=completed_recordings[0].sha256,
            )
            with database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO audio_assets (
                        asset_id, sha256, size_bytes, duration_ms, format,
                        media_id, legacy_ref, created_at
                    ) VALUES (?, ?, ?, ?, 'wav', ?, ?, ?)
                    """,
                    (
                        existing_asset_id,
                        completed_recordings[0].sha256,
                        completed_recordings[0].size,
                        6,
                        existing_media.media_id,
                        "historical-import:first",
                        "2026-01-01T00:00:00Z",
                    ),
                )
            ingest = V3UploadIngestAdapter(
                trust,
                lambda: SqliteUnitOfWork(database),
                audio_store,
                artifact_store,
            )

            first = ingest.ingest_completed(record.device_id, manifest_record, store)
            with patch.object(store, "list_uploads", side_effect=AssertionError("replay scanned inbox")):
                replay = ingest.ingest_completed(record.device_id, manifest_record, store)
            alternate_trust = TransferDeviceTrustAdapter(
                trust.authenticator,
                lambda: SqliteUnitOfWork(database),
                "b" * 64,
            )
            alternate_ref = (
                f"phone-upload:{'b' * 64}:{record.device_id}:"
                f"{manifest['sessionKey']}"
            )
            orphan_session_id = stable_ulid(alternate_ref)
            with database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO recording_sessions (
                        session_id, captured_start, captured_end, timezone,
                        state, revision, status_code, current_stage, progress,
                        blocking_reason, legacy_ref, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'admission_pending', 1,
                        'backup_required', 'backup_admission', 0.0,
                        'backup_restore_evidence_required', ?, ?, ?)
                    """,
                    (
                        orphan_session_id,
                        "2026-01-01T00:00:00Z",
                        "2026-01-01T00:00:01Z",
                        "Asia/Singapore",
                        alternate_ref,
                        "2026-01-01T00:00:00Z",
                        "2026-01-01T00:00:00Z",
                    ),
                )
            canonical_replay = V3UploadIngestAdapter(
                alternate_trust,
                lambda: SqliteUnitOfWork(database),
                audio_store,
                artifact_store,
            ).ingest_completed(record.device_id, manifest_record, store)

            self.assertEqual(first, replay)
            self.assertEqual(first["status"], "ingested")
            self.assertEqual(canonical_replay["status"], "already_ingested")
            self.assertEqual(canonical_replay["session_id"], first["session_id"])
            with database.read() as connection:
                counts = {
                    table: connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                    for table in (
                        "recording_sessions",
                        "audio_assets",
                        "audio_replicas",
                        "capture_segments",
                        "session_manifests",
                    )
                }
                changes = connection.execute(
                    "SELECT resource_type, payload_json "
                    "FROM change_events ORDER BY sequence"
                ).fetchall()
                reused_asset_id = connection.execute(
                    "SELECT asset_id FROM audio_replicas "
                    "WHERE legacy_ref LIKE ?",
                    ("%:chunk:0",),
                ).fetchone()[0]
                segment_bounds = connection.execute(
                    "SELECT session_start_ms, session_end_ms "
                    "FROM capture_segments ORDER BY sequence"
                ).fetchall()
                orphan = connection.execute(
                    "SELECT state, status_code, tombstoned_at "
                    "FROM recording_sessions WHERE session_id = ?",
                    (orphan_session_id,),
                ).fetchone()
                active_sessions = connection.execute(
                    "SELECT COUNT(*) FROM recording_sessions "
                    "WHERE tombstoned_at IS NULL"
                ).fetchone()[0]
            self.assertEqual(counts["recording_sessions"], 2)
            self.assertEqual(active_sessions, 1)
            self.assertEqual(counts["audio_assets"], 2)
            self.assertEqual(counts["audio_replicas"], 2)
            self.assertEqual(counts["capture_segments"], 2)
            self.assertEqual(counts["session_manifests"], 1)
            self.assertEqual(reused_asset_id, existing_asset_id)
            self.assertEqual(
                [tuple(row) for row in segment_bounds],
                [(0, 6), (6, 12)],
            )
            self.assertEqual(orphan["state"], "quarantined")
            self.assertEqual(orphan["status_code"], "stale")
            self.assertIsNotNone(orphan["tombstoned_at"])
            self.assertEqual(
                [row["resource_type"] for row in changes],
                [
                    "audio_asset",
                    "audio_asset",
                    "recording_session",
                    "recording_session",
                ],
            )
            session_projection = json.loads(changes[-2]["payload_json"])
            self.assertEqual(
                session_projection["session_key"],
                manifest["sessionKey"],
            )

            with SqliteUnitOfWork(database) as uow:
                uow.changes.append("recording_session", first["session_id"], 2, "upsert",
                                   {"session_id": first["session_id"], "state": "ready_for_processing"})
                latest = uow.changes.latest("recording_session", first["session_id"])
                self.assertEqual(latest.payload["session_key"], manifest["sessionKey"])
            # Reproduce a historical event without its identity, then run the repair.
            from allday_asr.v3.adapters.sqlite.migrations.v012_session_sync_identity import SQL
            with database.transaction() as connection:
                connection.execute("INSERT INTO change_events (resource_type, resource_id, revision, operation, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    ("recording_session", first["session_id"], 2, "upsert",
                     json.dumps({"session_id": first["session_id"], "state": "ready_for_processing"}), "2026-01-01T00:00:00Z"))
                connection.execute(SQL)
                connection.execute(SQL)
                repaired = connection.execute("SELECT payload_json FROM change_events WHERE resource_id = ? ORDER BY sequence DESC LIMIT 1", (first["session_id"],)).fetchone()
                self.assertEqual(json.loads(repaired[0])["session_key"], manifest["sessionKey"])


    def test_legacy_completion_confirms_identical_audio_without_reprocessing(self) -> None:
        for duplicate, tamper in ((False, False), (False, True), (True, False), (True, True)):
            with self.subTest(duplicate=duplicate, tamper=tamper), _workspace_directory() as root:
                database, _, device, trust, _, sync = self._environment(root)
                store = UploadStore(root / "inbox")
                artifacts = ContentAddressedStore(root / "artifacts")
                ingest = V3UploadIngestAdapter(
                    trust, lambda database=database: SqliteUnitOfWork(database),
                    ContentAddressedStore(root / "audio"), artifacts,
                )
                directory = "pcm_session_1767225600000"

                def upload(name, content, kind, store=store, directory=directory):
                    started, _ = store.create_upload(
                        relative_path=f"{directory}/{name}", size=len(content),
                        sha256=hashlib.sha256(content).hexdigest(), kind=kind,
                    )
                    return store.append_chunk(started.upload_id, offset=0, data=content)

                name = "segment_0_first_0.wav"
                audio_record = upload(name, b"a" * 244, "recording")
                legacy = {
                    "format": "AllDayRecording session manifest v1",
                    "sessionKey": "watch-session:1767225600000",
                    "sessionStartedAt": 1767225600000,
                    "device": "HUAWEI WATCH 5", "timezone": "Asia/Singapore",
                    "audio": {"sampleRate": 16000, "channels": 1, "bitsPerSample": 16},
                    "chunks": [{"index": 0, "fileName": name,
                                "firstSample": 0, "sampleCount": 100}],
                    "completedSegments": 1, "totalSamples": 100, "continuityValid": True,
                }
                completed = {**legacy, "format": "AllDayRecording session manifest v2",
                             "completion": {"source": "legacy_user_confirmed",
                                            "completedSegments": 1, "totalSamples": 100,
                                            "confirmedAt": 1767225601000}}
                initial_record = upload("session_summary.json", json.dumps(completed).encode(), "manifest")
                initial = ingest.ingest_completed(device.device_id, initial_record, store)
                # Reproduce a session admitted by the old V1 receiver.
                previous = legacy if not duplicate else {
                    "sample_rate": 16000, "channels": 1, "bits_per_sample": 16,
                    "chunk_count": 1, "duration_samples": 100,
                    "declared_continuity_valid": True,
                }
                legacy_record = upload("session_summary.json", json.dumps(previous).encode(), "manifest")
                stored = artifacts.put_file(store.completed_path(legacy_record))
                with database.transaction() as connection:
                    trigger = connection.execute(
                        "SELECT sql FROM sqlite_master WHERE name='protect_session_manifests_from_update'"
                    ).fetchone()[0]
                    connection.execute("DROP TRIGGER protect_session_manifests_from_update")
                    connection.execute(
                        "UPDATE session_manifests SET sha256=?, storage_ref=?, entries_json=? WHERE session_id=?",
                        (legacy_record.sha256, stored.storage_key, json.dumps(previous), initial["session_id"]),
                    )
                    connection.execute(trigger)
                    connection.execute(
                        "UPDATE idempotency_records SET response_json=? WHERE idempotency_key=?",
                        (json.dumps({**initial, "manifest_sha256": legacy_record.sha256}),
                         f"phone-manifest:{initial['session_id']}"),
                    )
                active_trust = trust
                if duplicate:
                    active_trust = TransferDeviceTrustAdapter(
                        trust.authenticator, lambda database=database: SqliteUnitOfWork(database), "b" * 64,
                    )
                    alias_ref = f"phone-upload:{'b' * 64}:{device.device_id}:{completed['sessionKey']}"
                    with SqliteUnitOfWork(database) as uow:
                        canonical = uow.catalog.get_session(initial["session_id"])
                        alias_id = stable_ulid(alias_ref)
                        uow.catalog.add_session(replace(canonical, session_id=alias_id, legacy_ref=alias_ref))
                        uow.catalog.tombstone_duplicate_session(alias_id, canonical.session_id, datetime.now(timezone.utc))
                    ingest = V3UploadIngestAdapter(
                        active_trust, lambda database=database: SqliteUnitOfWork(database),
                        ContentAddressedStore(root / "audio"), artifacts,
                    )
                if tamper:
                    with patch.object(store, "list_uploads", return_value=[replace(audio_record, sha256="f" * 64)]), self.assertRaisesRegex(UploadConflictError, "分片哈希"):
                        ingest.ingest_completed(device.device_id, initial_record, store)
                    continue
                workflow = MagicMock()
                gateway = DeviceGateway(active_trust, sync, ingest, workflow)
                confirmed = gateway.upload_completed(device.device_id, initial_record, store)
                self.assertEqual(confirmed["input_revision"], 1)
                self.assertTrue(confirmed["input_unchanged"])
                self.assertEqual(confirmed["session_id"], initial["session_id"])
                workflow.assert_not_called()
                restarted = V3UploadIngestAdapter(
                    active_trust, lambda database=database: SqliteUnitOfWork(database),
                    ContentAddressedStore(root / "audio"), artifacts,
                )
                self.assertEqual(restarted.ingest_completed(device.device_id, initial_record, store), confirmed)
                with database.read() as connection:
                    self.assertEqual(connection.execute("SELECT sha256 FROM session_manifests").fetchone()[0], legacy_record.sha256)
                    self.assertEqual(connection.execute("SELECT count(*) FROM session_manifest_revisions").fetchone()[0], 0)
                    self.assertEqual(connection.execute("SELECT count(*) FROM capture_segments").fetchone()[0], 1)
                    if duplicate:
                        self.assertEqual(connection.execute(
                            "SELECT state FROM recording_sessions WHERE session_id=?", (alias_id,)
                        ).fetchone()[0], "quarantined")
                changed = {**completed, "completion": {**completed["completion"], "confirmedAt": 1767225602000}}
                changed_record = upload("session_summary.json", json.dumps(changed).encode(), "manifest")
                self.assertEqual(ingest.ingest_completed(device.device_id, changed_record, store)["input_revision"], 1)
                if duplicate:
                    wrong_time = {**completed, "sessionStartedAt": completed["sessionStartedAt"] + 1}
                    wrong_record = upload("session_summary.json", json.dumps(wrong_time).encode(), "manifest")
                    with self.assertRaisesRegex(UploadConflictError, "开始时间"):
                        ingest.ingest_completed(device.device_id, wrong_record, store)

    def test_manifest_append_preserves_original_and_rejects_changed_prefix(self) -> None:
        with _workspace_directory() as root:
            database, _, device, trust, _, _ = self._environment(root)
            store = UploadStore(root / "inbox")
            ingest = V3UploadIngestAdapter(
                trust, lambda: SqliteUnitOfWork(database),
                ContentAddressedStore(root / "audio"),
                ContentAddressedStore(root / "artifacts"),
            )
            directory = "pcm_session_1767225600000"

            def upload(path: str, content: bytes, kind: str) -> UploadRecord:
                started, _ = store.create_upload(
                    relative_path=f"{directory}/{path}", size=len(content),
                    sha256=hashlib.sha256(content).hexdigest(), kind=kind,
                )
                return store.append_chunk(started.upload_id, offset=0, data=content)

            first_name = "segment_0_first_0.wav"
            second_name = "segment_1_first_100.wav"
            upload(first_name, b"a" * 244, "recording")
            first_chunk = {"index": 0, "fileName": first_name,
                           "firstSample": 0, "sampleCount": 100}
            manifest = {
                "format": "AllDayRecording session manifest v2",
                "sessionKey": "watch-session:1767225600000",
                "sessionStartedAt": 1767225600000,
                "device": "HUAWEI WATCH 5", "timezone": "Asia/Singapore",
                "audio": {"sampleRate": 16000, "channels": 1, "bitsPerSample": 16},
                "chunks": [first_chunk], "completedSegments": 1,
                "totalSamples": 100, "continuityValid": True,
                "completion": {"source": "watch_stop", "completedSegments": 1,
                               "totalSamples": 100, "confirmedAt": 1767225601000},
            }

            def submit(value: dict) -> tuple[dict, UploadRecord]:
                encoded = json.dumps(value).encode()
                record = upload("session_summary.json", encoded, "manifest")
                return ingest.ingest_completed(device.device_id, record, store), record

            original, original_record = submit(manifest)
            backup_adapter = FilesystemSessionBackupAdapter(
                database, ContentAddressedStore(root / "audio"),
                ContentAddressedStore(root / "artifacts"),
            )
            first_backup = backup_adapter.backup(
                original["session_id"], root / "independent-backups",
                storage_kind="independent_device",
            )
            first_manifest_bytes = (first_backup.destination / "manifest" /
                                    "session_summary.json").read_bytes()
            upload(second_name, b"b" * 244, "recording")
            second_chunk = {"index": 1, "fileName": second_name,
                            "firstSample": 100, "sampleCount": 100}
            appended_manifest = {**manifest, "chunks": [first_chunk, second_chunk],
                                 "completedSegments": 2, "totalSamples": 200,
                                 "completion": {**manifest["completion"],
                                                "completedSegments": 2, "totalSamples": 200}}
            appended, appended_record = submit(appended_manifest)
            second_backup = backup_adapter.backup(
                original["session_id"], root / "independent-backups",
                storage_kind="independent_device",
            )
            retry_backup = backup_adapter.backup(
                original["session_id"], root / "independent-backups",
                storage_kind="independent_device",
            )
            self.assertEqual((first_backup.input_revision, second_backup.input_revision), (1, 2))
            self.assertEqual(second_backup.manifest_sha256, appended_record.sha256)
            self.assertNotEqual(first_backup.destination, second_backup.destination)
            self.assertEqual(retry_backup.destination, second_backup.destination)
            self.assertEqual(retry_backup.digest, second_backup.digest)
            self.assertEqual((first_backup.destination / "manifest" /
                              "session_summary.json").read_bytes(), first_manifest_bytes)
            replay = ingest.ingest_completed(device.device_id, appended_record, store)
            restarted = V3UploadIngestAdapter(
                trust, lambda: SqliteUnitOfWork(database),
                ContentAddressedStore(root / "audio"),
                ContentAddressedStore(root / "artifacts"),
            ).ingest_completed(device.device_id, appended_record, UploadStore(root / "inbox"))
            self.assertEqual(appended["session_id"], original["session_id"])
            self.assertEqual(appended["input_revision"], 2)
            self.assertEqual(replay["input_revision"], 2)
            self.assertEqual(restarted["input_revision"], 2)
            with database.read() as connection:
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM capture_segments WHERE session_id=?",
                    (original["session_id"],)).fetchone()[0], 2)
                self.assertEqual(connection.execute(
                    "SELECT sha256 FROM session_manifests WHERE session_id=?",
                    (original["session_id"],)).fetchone()[0], original_record.sha256)
                self.assertEqual(connection.execute(
                    "SELECT sha256 FROM session_manifest_revisions WHERE session_id=?",
                    (original["session_id"],)).fetchone()[0], appended_record.sha256)
            old_run = SimpleNamespace(
                job=SimpleNamespace(status="succeeded"),
                run=SimpleNamespace(session_id=original["session_id"],
                                    pipeline_version="v3-native.1", input_revision=1),
            )
            runner = object.__new__(V3AutomaticWorkflowRunner)
            runner.core = SimpleNamespace(database=database,
                processing=SimpleNamespace(get=lambda _job_id: old_run))
            runner.pipeline_version = "v3-native.1"
            runner.status = lambda _session_id: {"job_id": "old-job"}
            self.assertEqual(runner._input_revision(original["session_id"]), 2)
            self.assertEqual(runner._manifest_sha256(original["session_id"]), appended_record.sha256)
            self.assertIsNone(runner._completed_processing_snapshot(original["session_id"]))
            # A watch stop record can arrive after a manual completion. Its
            # source/timestamp change must not replace identical audio inputs.
            reconfirmed_manifest = {
                **appended_manifest,
                "completion": {**appended_manifest["completion"],
                               "source": "legacy_user_confirmed",
                               "confirmedAt": 1767225602000},
            }
            reconfirmed, reconfirmed_record = submit(reconfirmed_manifest)
            self.assertTrue(reconfirmed["input_unchanged"])
            self.assertEqual(reconfirmed["input_revision"], 2)
            self.assertEqual(reconfirmed["session_id"], original["session_id"])
            self.assertEqual(reconfirmed["manifest_sha256"], reconfirmed_record.sha256)
            reconfirmed_retry = V3UploadIngestAdapter(
                trust, lambda: SqliteUnitOfWork(database),
                ContentAddressedStore(root / "audio"),
                ContentAddressedStore(root / "artifacts"),
            ).ingest_completed(device.device_id, reconfirmed_record, UploadStore(root / "inbox"))
            self.assertEqual(reconfirmed_retry, reconfirmed)
            self.assertEqual(runner._manifest_sha256(original["session_id"]), appended_record.sha256)
            self.assertEqual(backup_adapter.backup(
                original["session_id"], root / "independent-backups",
                storage_kind="independent_device",
            ).destination, second_backup.destination)
            changed = {**appended_manifest,
                       "chunks": [{**first_chunk, "index": 5},
                                  {**second_chunk, "index": 6}]}
            with self.assertRaisesRegex(UploadConflictError, "旧分片"):
                submit(changed)
            with self.assertRaisesRegex(UploadConflictError, "不同内容"):
                upload(first_name, b"changed" * 34 + b"------", "recording")
            third_name = "segment_2_first_200.wav"
            upload(third_name, b"c" * 244, "recording")
            third_chunk = {"index": 2, "fileName": third_name,
                           "firstSample": 200, "sampleCount": 100}
            ambiguous = {**appended_manifest,
                         "chunks": [first_chunk, second_chunk, third_chunk],
                         "completedSegments": 3, "totalSamples": 300,
                         "completion": {**manifest["completion"],
                                        "completedSegments": 3, "totalSamples": 300}}
            third, _ = submit(ambiguous)
            self.assertEqual(third["input_revision"], 3)
            with database.read() as connection:
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM session_manifest_revisions WHERE session_id=?",
                    (original["session_id"],)).fetchone()[0], 2)

    def test_http_sync_requires_one_time_device_signature(self) -> None:
        with _workspace_directory() as root:
            _, private_key, record, trust, handler, service = self._environment(root)
            server = create_transfer_server(
                inbox=root / "inbox",
                host="127.0.0.1",
                port=0,
                token="pairing-code-123456789",
                allow_insecure_http=True,
                device_manager=trust,
                receiver_id=RECEIVER_ID,
                v3_gateway=DeviceGateway(trust, service),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.port}"
            payload = {
                "projection_version": PROJECTION_VERSION,
                "cursor": None,
                "client_operations": [
                    {
                        "operation_id": stable_ulid("http-operation", "one"),
                        "kind": "review.resolve",
                        "base_revision": 1,
                        "payload": {"decision": "accepted"},
                    }
                ],
                "pull_limit": 100,
            }
            raw = json.dumps(payload, separators=(",", ":")).encode()
            binding = RequestBinding.for_request(
                method="POST", path="/device/v3/sync", body=raw
            )
            try:
                challenge_request = urllib.request.Request(
                    base_url + "/api/v1/devices/authenticate/challenge",
                    data=json.dumps(
                        {
                            "device_id": record.device_id,
                            "request": {
                                "method": binding.method,
                                "path": binding.path,
                                "body_sha256": binding.body_sha256,
                                "upload_offset": binding.upload_offset,
                            },
                        }
                    ).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(challenge_request, timeout=3) as response:
                    challenge = json.loads(response.read())
                signature = private_key.sign(
                    build_device_signature_payload(
                        challenge_id=challenge["challenge_id"],
                        nonce=challenge["nonce"],
                        binding=binding,
                    ),
                    ec.ECDSA(hashes.SHA256()),
                )
                request = urllib.request.Request(
                    base_url + "/device/v3/sync",
                    data=raw,
                    headers={
                        "Content-Type": "application/json",
                        "X-AllDay-Device-ID": record.device_id,
                        "X-AllDay-Device-Challenge": challenge["challenge_id"],
                        "X-AllDay-Device-Signature": _b64(signature),
                    },
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=3) as response:
                    result = json.loads(response.read())
                self.assertEqual(
                    result["projection_version"], PROJECTION_VERSION
                )
                self.assertEqual(result["receipts"][0]["status"], "applied")
                self.assertEqual(handler.calls, 1)

                with self.assertRaises(urllib.error.HTTPError) as replay:
                    urllib.request.urlopen(request, timeout=3)
                self.assertEqual(replay.exception.code, HTTPStatus.UNAUTHORIZED)
                error = json.loads(replay.exception.read())
                self.assertEqual(error["code"], "UNAUTHORIZED")
                self.assertEqual(len(error["request_id"]), 26)

                arbitrary = urllib.request.Request(
                    base_url + "/api/v3/admin",
                    headers={"Authorization": "Bearer pairing-code-123456789"},
                )
                with self.assertRaises(urllib.error.HTTPError) as missing:
                    urllib.request.urlopen(arbitrary, timeout=3)
                self.assertEqual(missing.exception.code, HTTPStatus.NOT_FOUND)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
