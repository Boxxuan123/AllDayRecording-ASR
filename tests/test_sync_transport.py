import gzip
import http.client
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from tests import test_v3_device_sync as sync_fixture
from tests.test_transfer_tls_admission import running_server
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
from allday_asr.v3.adapters.sqlite.operational_repositories import (
    SqliteChangeLogRepository,
)
from allday_asr.v3.application.mobile_sync import MobileSyncService
from allday_asr.v3.domain.device_sync import PROJECTION_VERSION, SyncRequest
from allday_asr.v3.domain.ids import stable_ulid
from allday_asr.v3.interfaces.transfer.http_handler import (
    TransferRequestHandler,
    accepts_gzip,
)


@pytest.fixture
def env():
    with sync_fixture._workspace_directory() as root:
        database, _, record, trust, _, service = sync_fixture.V3DeviceSyncTests()._environment(root)
        yield database, trust.domain_device_id(record.device_id), service


def append(db, resource, revision, payload=None, operation="upsert"):
    with SqliteUnitOfWork(db) as uow:
        return uow.changes.append(
            "recording_session",
            stable_ulid("snapshot", str(resource)),
            revision,
            operation,
            payload or {"revision": revision},
        )


def test_watermarked_snapshot_restart_concurrent_changes_and_deletes(env):
    db, device, sync = env
    with SqliteUnitOfWork(db) as uow:
        for revision in range(1, 21):
            for resource in range(60):
                uow.changes.append(
                    "recording_session",
                    stable_ulid("snapshot", str(resource)),
                    revision,
                    "upsert",
                    {"revision": revision},
                )
    first = sync.synchronize(
        device, SyncRequest(PROJECTION_VERSION, None, (), 7, bootstrap=True)
    )
    assert first.next_cursor.startswith("snapshot-v1-1200-")
    snapshot = list(first.changes)
    append(db, 0, 21)
    append(db, 59, 21, operation="tombstone")
    append(db, 60, 1)
    sync = MobileSyncService(lambda: SqliteUnitOfWork(db))
    page = first
    while page.next_cursor.startswith("snapshot-"):
        page = sync.synchronize(
            device, SyncRequest(PROJECTION_VERSION, page.next_cursor, (), 7)
        )
        snapshot.extend(page.changes)
    assert page.next_cursor == "cursor-1200" and page.has_more
    assert len(snapshot) == 60 and all(c.revision == 20 for c in snapshot)
    assert len({c.resource_id for c in snapshot}) == 60
    delta = sync.synchronize(
        device, SyncRequest(PROJECTION_VERSION, page.next_cursor, (), 500)
    )
    assert [c.revision for c in delta.changes] == [21, 21, 1]
    assert delta.changes[1].operation == "tombstone" and not delta.has_more
    state = {
        c.resource_id: (c.operation, c.revision, c.resource)
        for c in snapshot + list(delta.changes)
    }
    legacy = sync.synchronize(device, SyncRequest(PROJECTION_VERSION, None, (), 500))
    history = list(legacy.changes)
    while legacy.has_more:
        legacy = sync.synchronize(
            device, SyncRequest(PROJECTION_VERSION, legacy.next_cursor, (), 500)
        )
        history.extend(legacy.changes)
    assert state == {
        c.resource_id: (c.operation, c.revision, c.resource) for c in history
    }


def test_byte_budget_cursor_progress_and_oversized_single_resource(env):
    db, device, sync = env
    for n in range(4):
        append(db, n, 1, {"text": "中" * 3000})
    first = sync.synchronize(
        device, SyncRequest(PROJECTION_VERSION, None, (), 500, pull_bytes=16384)
    )
    assert (
        len(first.changes) == 1 and first.next_cursor == "cursor-1" and first.has_more
    )
    assert len(json.dumps(first.as_dict(), ensure_ascii=False).encode()) < 16384
    assert first.transaction_ms is not None and first.transaction_ms >= 0
    append(db, 9, 1, {"text": "中" * 10000})
    last = sync.synchronize(
        device, SyncRequest(PROJECTION_VERSION, "cursor-4", (), 500, pull_bytes=16384)
    )
    assert (
        len(last.changes) == 1 and last.next_cursor == "cursor-5" and not last.has_more
    )
    with pytest.raises(ValueError):
        sync.synchronize(
            device, SyncRequest(PROJECTION_VERSION, "snapshot-v1-999-0", (), 500)
        )


def test_snapshot_read_does_not_hold_writer(env):
    db, device, sync = env
    append(db, 1, 1)
    original = SqliteChangeLogRepository.snapshot_after
    with ThreadPoolExecutor(1) as pool:

        def concurrent(repo, *args):
            pool.submit(append, db, 2, 1).result(timeout=2)
            return original(repo, *args)

        with patch.object(SqliteChangeLogRepository, "snapshot_after", concurrent):
            response = sync.synchronize(
                device, SyncRequest(PROJECTION_VERSION, None, (), 500, bootstrap=True)
            )
    assert len(response.changes) == 1 and response.next_cursor == "cursor-1"
    assert (
        len(
            sync.synchronize(
                device, SyncRequest(PROJECTION_VERSION, "cursor-1", (), 500)
            ).changes
        )
        == 1
    )


def test_real_tls_connection_reuse_and_compression(monkeypatch):
    payload = {"items": [{"text": "合成同步数据" * 1000} for _ in range(20)]}
    monkeypatch.setattr(
        TransferRequestHandler, "_dispatch_get", lambda h: h._send_json(200, payload)
    )
    with running_server() as (server, context):
        client = http.client.HTTPSConnection(
            "127.0.0.1", server.port, context=context, timeout=3
        )
        try:
            client.request("GET", "/synthetic", headers={"Accept-Encoding": "gzip"})
            response = client.getresponse()
            wire = response.read()
            connection = response.getheader("X-AllDay-Connection-ID")
            assert (
                response.version == 11
                and response.getheader("Content-Encoding") == "gzip"
            )
            assert len(wire) == int(response.getheader("Content-Length"))
            assert json.loads(gzip.decompress(wire)) == payload
            assert len(wire) < int(response.getheader("X-AllDay-JSON-Bytes")) // 10
            socket = client.sock
            for encoding in ["identity", "gzip;q=0, *;q=1", "gzip"]:
                client.request(
                    "GET", "/synthetic", headers={"Accept-Encoding": encoding}
                )
                response = client.getresponse()
                data = response.read()
                assert (
                    client.sock is socket
                    and response.getheader("X-AllDay-Connection-ID") == connection
                )
                assert (response.getheader("Content-Encoding") == "gzip") == (
                    encoding == "gzip"
                )
                assert (
                    json.loads(gzip.decompress(data) if encoding == "gzip" else data)
                    == payload
                )
        finally:
            client.close()


def test_unconsumed_rejected_body_closes_persistent_connection():
    with running_server() as (server, context):
        client = http.client.HTTPSConnection(
            "127.0.0.1", server.port, context=context, timeout=3
        )
        try:
            client.request(
                "POST", "/unknown", body=b"GET /api/v1/status HTTP/1.1\r\n\r\n"
            )
            response = client.getresponse()
            response.read()
            assert (
                response.status == 404 and response.getheader("Connection") == "close"
            )
            assert client.sock is None
        finally:
            client.close()


@pytest.mark.parametrize(
    "value,expected",
    [
        ("gzip", True),
        ("GZip;q=0.5", True),
        ("*", True),
        ("gzip;q=0,*;q=1", False),
        ("gzip;q=invalid", False),
        ("br", False),
    ],
)
def test_accept_encoding(value, expected):
    assert accepts_gzip(value) is expected
