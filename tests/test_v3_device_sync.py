from __future__ import annotations

import base64
import hashlib
import json
import shutil
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from http import HTTPStatus
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from allday_asr.interfaces.transfer.devices import (
    DEVICE_ALGORITHM,
    DeviceAuthManager,
    DeviceCredentialStore,
    DeviceForbiddenError,
    DeviceUnauthorizedError,
    build_device_signature_payload,
)
from allday_asr.interfaces.transfer.passkeys import RequestBinding
from allday_asr.interfaces.transfer.server import create_transfer_server
from allday_asr.interfaces.transfer.store import UploadStore
from allday_asr.v3.adapters.files import ContentAddressedStore
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork, V3Database
from allday_asr.v3.adapters.transfer import (
    TransferDeviceTrustAdapter,
    V3UploadIngestAdapter,
)
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


class V3DeviceSyncTests(unittest.TestCase):
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
                projection_version=1,
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
                    projection_version=1,
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

    def test_completed_manifest_admits_audio_and_is_idempotent(self) -> None:
        with _workspace_directory() as root:
            database, _, record, trust, _, _ = self._environment(root)
            store = UploadStore(root / "inbox")
            audio_store = ContentAddressedStore(root / "audio")
            artifact_store = ContentAddressedStore(root / "artifacts")
            directory = "pcm_session_1767225600000"
            chunks = []
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
                chunks.append(
                    {
                        "index": index,
                        "fileName": name,
                        "firstSample": index * 100,
                        "sampleCount": 100,
                    }
                )
            manifest = {
                "format": "AllDayRecording session manifest v1",
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
            ingest = V3UploadIngestAdapter(
                trust,
                lambda: SqliteUnitOfWork(database),
                audio_store,
                artifact_store,
            )

            first = ingest.ingest_completed(record.device_id, manifest_record, store)
            replay = ingest.ingest_completed(record.device_id, manifest_record, store)

            self.assertEqual(first, replay)
            self.assertEqual(first["status"], "ingested")
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
                    "SELECT resource_type FROM change_events ORDER BY sequence"
                ).fetchall()
            self.assertEqual(counts["recording_sessions"], 1)
            self.assertEqual(counts["audio_assets"], 2)
            self.assertEqual(counts["audio_replicas"], 2)
            self.assertEqual(counts["capture_segments"], 2)
            self.assertEqual(counts["session_manifests"], 1)
            self.assertEqual(
                [row["resource_type"] for row in changes],
                ["audio_asset", "audio_asset", "recording_session"],
            )

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
                "projection_version": 1,
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
                self.assertEqual(result["projection_version"], 1)
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
