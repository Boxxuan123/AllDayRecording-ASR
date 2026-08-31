from __future__ import annotations

import json
import shutil
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
from allday_asr.v3.application import (
    RecordBackupEvidenceCommand,
    SubmitProcessingCommand,
)
from allday_asr.v3.bootstrap import V3CorePaths
from allday_asr.v3.domain import (
    AudioAsset,
    AudioFormat,
    AudioReplica,
    AudioReplicaState,
    Artifact,
    CaptureSegment,
    Device,
    DeviceKind,
    DeviceStatus,
    RecordingSession,
    RecordingSessionState,
    SessionManifest,
)
from allday_asr.v3.interfaces.desktop_server import create_v3_desktop_server


TEST_STATE = Path(__file__).parents[1] / "state"


class V3DesktopApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TEST_STATE / f"desktop-api-{uuid4().hex}"
        self.paths = V3CorePaths.from_state_dir(self.root)
        self.token = "desktop-test-token"
        self.server = create_v3_desktop_server(
            paths=self.paths, port=0, token=self.token
        )
        self._seed()
        self._start_server()

    def tearDown(self) -> None:
        self._stop_server()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_authentication_error_shape_and_static_spa_routes(self) -> None:
        status, payload, headers = self._request("/api/v3/status", authenticated=False)
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "desktop_session_required")
        self.assertEqual(set(payload), {"code", "message", "details", "request_id"})
        self.assertIn("nosniff", headers["X-Content-Type-Options"])

        status, html, _ = self._request("/recordings/session-1?tab=evidence")
        self.assertEqual(status, 200)
        self.assertIn("AllDay Recording V3", html)
        self.assertNotIn("workspace/controller", html)

    def test_queries_keyset_pagination_and_byte_range_media(self) -> None:
        status, value, _ = self._request("/api/v3/status")
        self.assertEqual(status, 200)
        self.assertEqual(value["state"], "ready")

        _, overview, _ = self._request("/api/v3/overview")
        self.assertEqual(overview["counts"]["sessions"], 2)
        self.assertEqual(overview["counts"]["backed_up_sessions"], 1)

        _, first, _ = self._request("/api/v3/recording-sessions?limit=1")
        self.assertEqual(len(first["items"]), 1)
        self.assertIsNotNone(first["next_cursor"])
        _, second, _ = self._request(
            f"/api/v3/recording-sessions?limit=1&cursor={first['next_cursor']}"
        )
        self.assertEqual(len(second["items"]), 1)
        self.assertNotEqual(
            first["items"][0]["session_id"], second["items"][0]["session_id"]
        )

        _, detail, _ = self._request("/api/v3/recording-sessions/session-1")
        self.assertEqual(detail["session"]["session_id"], "session-1")
        self.assertEqual(detail["segments"][0]["media_id"], self.media_id)

        status, media, headers = self._request(
            f"/api/v3/media/{self.media_id}", headers={"Range": "bytes=2-5"}
        )
        self.assertEqual(status, 206)
        self.assertEqual(media, b"2345")
        self.assertEqual(headers["Content-Range"], "bytes 2-5/10")

    def test_durable_job_command_sse_projection_and_restart_recovery(self) -> None:
        snapshot = self.server.application.core.processing.submit(
            SubmitProcessingCommand(
                session_id="session-1",
                pipeline_version="v3-desktop-test.1",
                input_revision=1,
                config={"profile": "desktop-test"},
            )
        )
        job_id = snapshot.job.job_id
        with SqliteUnitOfWork(self.server.application.core.database) as uow:
            uow.artifacts.add(
                Artifact(
                    artifact_id="artifact-missing",
                    run_id=snapshot.run.run_id,
                    kind="legacy_missing",
                    producer="desktop-test",
                    producer_version="1",
                    config_digest=snapshot.run.config_digest,
                    input_refs=(),
                    storage_ref="legacy-missing:test",
                    sha256=None,
                    size_bytes=None,
                    status="quarantined",
                    metadata={},
                    created_at=datetime.now(timezone.utc),
                )
            )
        detail = self.server.application.core.desktop.session_detail("session-1")
        missing = next(
            item for item in detail["artifacts"] if item["artifact_id"] == "artifact-missing"
        )
        self.assertIsNone(missing["sha256"])
        self.assertIsNone(missing["size_bytes"])
        _, jobs, _ = self._request("/api/v3/processing-jobs")
        self.assertEqual(jobs["items"][0]["job_id"], job_id)
        events = self.server.application.core.desktop.processing_events(0)
        self.assertTrue(any(event["resource_id"] == snapshot.run.run_id for event in events))

        status, cancelled, _ = self._request(
            f"/api/v3/processing-jobs/{job_id}/cancel",
            method="POST",
            body={"reason": "desktop test"},
            headers={"Origin": self.server.application.base_url},
        )
        self.assertEqual(status, 202)
        self.assertIn(cancelled["job"]["status"], {"cancel_requested", "cancelled"})

        self._stop_server()
        self.server = create_v3_desktop_server(
            paths=self.paths, port=0, token=self.token
        )
        self._start_server()
        _, recovered, _ = self._request(f"/api/v3/processing-jobs/{job_id}")
        self.assertEqual(recovered["job"]["job_id"], job_id)
        self.assertIn(recovered["job"]["status"], {"cancel_requested", "cancelled"})

    def test_mutations_reject_cross_origin_requests(self) -> None:
        status, payload, _ = self._request(
            "/api/v3/processing-jobs/missing/cancel",
            method="POST",
            body={"reason": "denied"},
            headers={"Origin": "https://example.invalid"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "cross_origin_denied")

    def _start_server(self) -> None:
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()

    def _stop_server(self) -> None:
        server = getattr(self, "server", None)
        thread = getattr(self, "thread", None)
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)
        self.thread = None

    def _request(
        self,
        path: str,
        *,
        authenticated: bool = True,
        method: str = "GET",
        body: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, object, object]:
        request_headers = dict(headers or {})
        if authenticated:
            request_headers["X-AllDay-Desktop-Session"] = self.token
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.server.application.base_url}{path}",
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            payload = response.read()
            content_type = response.headers.get_content_type()
            if content_type == "application/json":
                decoded: object = json.loads(payload)
            elif content_type.startswith("text/"):
                decoded = payload.decode("utf-8")
            else:
                decoded = payload
            return response.status, decoded, response.headers

    def _seed(self) -> None:
        core = self.server.application.core
        now = datetime(2026, 8, 31, 1, 5, tzinfo=timezone.utc)
        audio = core.audio_store.put_bytes(b"0123456789")
        self.media_id = audio.media_id
        manifests = [
            core.artifact_store.put_bytes(
                json.dumps({"session_id": session_id}).encode("utf-8")
            )
            for session_id in ("session-1", "session-2")
        ]
        with SqliteUnitOfWork(core.database) as uow:
            uow.devices.add(
                Device(
                    device_id="computer-1",
                    kind=DeviceKind.COMPUTER,
                    name="Desktop",
                    status=DeviceStatus.ACTIVE,
                    revision=1,
                    last_seen_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            uow.catalog.add_asset(
                AudioAsset(
                    asset_id="asset-1",
                    sha256=audio.sha256,
                    size_bytes=audio.size_bytes,
                    duration_ms=1000,
                    format=AudioFormat.WAV,
                    media_id=audio.media_id,
                    created_at=now,
                )
            )
            uow.catalog.add_replica(
                AudioReplica(
                    replica_id="replica-1",
                    asset_id="asset-1",
                    device_id="computer-1",
                    storage_key=audio.storage_key,
                    state=AudioReplicaState.AVAILABLE,
                    verified_at=now,
                    created_at=now,
                )
            )
            for index, session_id in enumerate(("session-1", "session-2")):
                captured = now - timedelta(hours=index)
                uow.catalog.add_session(
                    RecordingSession(
                        session_id=session_id,
                        captured_start=captured,
                        captured_end=captured + timedelta(seconds=1),
                        timezone="Asia/Singapore",
                        state=RecordingSessionState.COMPUTER_INGESTED,
                        revision=1,
                        status_code="backup_required",
                        current_stage="ingest",
                        progress=1.0,
                        blocking_reason="backup_required",
                        created_at=captured,
                        updated_at=captured,
                    )
                )
                uow.catalog.add_segment(
                    CaptureSegment(
                        segment_id=f"segment-{index + 1}",
                        session_id=session_id,
                        asset_id="asset-1",
                        replica_id="replica-1",
                        sequence=0,
                        session_start_ms=0,
                        session_end_ms=1000,
                        source_start_ms=0,
                        source_end_ms=1000,
                        start_sample=0,
                        captured_at=captured,
                    )
                )
                manifest = manifests[index]
                uow.catalog.add_manifest(
                    SessionManifest(
                        manifest_id=f"manifest-{index + 1}",
                        session_id=session_id,
                        schema_version="1",
                        sha256=manifest.sha256,
                        storage_ref=manifest.storage_key,
                        entries={"session_id": session_id},
                        created_at=captured,
                    )
                )
        admitted = core.admission.record_verified_backup(
            RecordBackupEvidenceCommand(
                session_id="session-1",
                provider="desktop-test",
                storage_kind="independent_device",
                digest="a" * 64,
                restore_checked_at=now,
                metadata={"restore_drill": True},
            )
        )
        self.assertTrue(admitted)


if __name__ == "__main__":
    unittest.main()
