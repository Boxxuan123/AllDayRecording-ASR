from __future__ import annotations

import shutil
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork, V3Database
from allday_asr.v3.domain.models import (
    Artifact,
    AudioAsset,
    AudioFormat,
    AudioReplica,
    AudioReplicaState,
    CaptureSegment,
    CorrectionOperation,
    Device,
    DeviceKind,
    DeviceStatus,
    ProcessingRun,
    ProcessingStatus,
    RecordingSession,
    RecordingSessionState,
    SessionManifest,
)


TEST_ROOT = Path(__file__).parent
NOW = datetime(2026, 8, 31, 1, 2, 3, tzinfo=timezone.utc)
NOW_TEXT = "2026-08-31T01:02:03Z"


class V3RepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TEST_ROOT / f"v3b-repository-{uuid4().hex}"
        self.database = V3Database.open(self.directory / "core.sqlite3")

    def tearDown(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_catalog_and_supporting_infrastructure_persist_atomically(self) -> None:
        device, session, asset, replica, segment = _catalog_graph()
        manifest = SessionManifest(
            manifest_id="manifest-1",
            session_id=session.session_id,
            schema_version="2",
            sha256="b" * 64,
            storage_ref="bb/bb/" + "b" * 64,
            entries={"count": 1},
            created_at=NOW,
        )
        run = ProcessingRun(
            run_id="run-1",
            session_id=session.session_id,
            pipeline_version="legacy-1",
            input_revision=1,
            status=ProcessingStatus.SUCCEEDED,
            config_digest="c" * 64,
            current_stage="complete",
            progress=1.0,
            created_at=NOW,
            updated_at=NOW,
            completed_at=NOW,
        )
        artifact = Artifact(
            artifact_id="artifact-1",
            run_id=run.run_id,
            kind="transcript",
            producer="test",
            producer_version="1",
            config_digest="c" * 64,
            input_refs=(asset.asset_id,),
            storage_ref="cc/cc/" + "c" * 64,
            sha256="c" * 64,
            size_bytes=3,
            status="active",
            metadata={"language": "zh"},
            created_at=NOW,
        )
        correction = CorrectionOperation(
            correction_id="correction-1",
            target_type="utterance",
            target_id="utterance-1",
            before_revision=1,
            patch={"text": "修正"},
            actor="user",
            created_at=NOW,
        )

        with SqliteUnitOfWork(self.database, now=lambda: NOW_TEXT) as uow:
            self.assertTrue(uow.devices.add(device))
            self.assertTrue(uow.catalog.add_session(session))
            self.assertTrue(uow.catalog.add_asset(asset))
            self.assertTrue(uow.catalog.add_replica(replica))
            self.assertTrue(uow.catalog.add_segment(segment))
            self.assertTrue(uow.catalog.add_manifest(manifest))
            self.assertTrue(uow.processing_runs.add(run))
            self.assertTrue(uow.artifacts.add(artifact))
            self.assertTrue(uow.corrections.add(correction))
            self.assertEqual(
                uow.changes.append("recording_session", session.session_id, 1, "upsert", {}),
                1,
            )
            self.assertTrue(
                uow.audit.append("session.import", "system", "session", session.session_id, {})
            )
            self.assertTrue(uow.idempotency.begin("operation-1", "import"))
            self.assertFalse(uow.idempotency.begin("operation-1", "import"))
            with self.assertRaisesRegex(ValueError, "another command"):
                uow.idempotency.begin("operation-1", "delete")
            uow.idempotency.complete("operation-1", {"ok": True})
            self.assertEqual(uow.idempotency.response("operation-1"), {"ok": True})
            self.assertTrue(
                uow.tombstones.add("obsolete_projection", "projection-1", 2, "stale")
            )

        with self.database.read() as connection:
            counts = {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "devices",
                    "recording_sessions",
                    "audio_assets",
                    "audio_replicas",
                    "capture_segments",
                    "session_manifests",
                    "processing_runs",
                    "artifacts",
                    "correction_operations",
                    "change_events",
                    "audit_entries",
                    "idempotency_records",
                    "tombstones",
                )
            }
        self.assertEqual(set(counts.values()), {1})

    def test_duplicates_are_idempotent_but_invalid_rows_raise(self) -> None:
        device, session, asset, replica, segment = _catalog_graph()
        with SqliteUnitOfWork(self.database, now=lambda: NOW_TEXT) as uow:
            self.assertTrue(uow.devices.add(device))
            self.assertTrue(uow.catalog.add_session(session))
            self.assertTrue(uow.catalog.add_asset(asset))
            self.assertTrue(uow.catalog.add_replica(replica))
            self.assertTrue(uow.catalog.add_segment(segment))

        with SqliteUnitOfWork(self.database, now=lambda: NOW_TEXT) as uow:
            self.assertFalse(uow.devices.add(device))
            self.assertFalse(uow.catalog.add_session(session))
            self.assertFalse(uow.catalog.add_asset(asset))
            self.assertFalse(uow.catalog.add_replica(replica))
            self.assertFalse(uow.catalog.add_segment(segment))

        invalid = RecordingSession(
            session_id="invalid-session",
            captured_start=NOW,
            captured_end=NOW + timedelta(seconds=1),
            timezone="UTC",
            state=RecordingSessionState.SEALED,
            revision=1,
            status_code="not-a-v3-status",
            current_stage=None,
            progress=0.0,
            blocking_reason=None,
            created_at=NOW,
            updated_at=NOW,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            with SqliteUnitOfWork(self.database, now=lambda: NOW_TEXT) as uow:
                uow.catalog.add_session(invalid)

    def test_immutable_evidence_tables_reject_update_and_delete(self) -> None:
        device, session, asset, replica, segment = _catalog_graph()
        with SqliteUnitOfWork(self.database, now=lambda: NOW_TEXT) as uow:
            uow.devices.add(device)
            uow.catalog.add_session(session)
            uow.catalog.add_asset(asset)
            uow.catalog.add_replica(replica)
            uow.catalog.add_segment(segment)
            uow.tombstones.add("projection", "projection-1", 2, "stale")

        for sql in (
            "UPDATE audio_assets SET duration_ms = 1 WHERE asset_id = 'asset-1'",
            "DELETE FROM capture_segments WHERE segment_id = 'segment-1'",
            "UPDATE tombstones SET revision = 3 WHERE resource_id = 'projection-1'",
        ):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                with self.database.transaction() as connection:
                    connection.execute(sql)


def _catalog_graph(
) -> tuple[Device, RecordingSession, AudioAsset, AudioReplica, CaptureSegment]:
    device = Device(
        device_id="device-1",
        kind=DeviceKind.COMPUTER,
        name="Computer",
        status=DeviceStatus.ACTIVE,
        revision=1,
        last_seen_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    session = RecordingSession(
        session_id="session-1",
        captured_start=NOW,
        captured_end=NOW + timedelta(seconds=2),
        timezone="Asia/Singapore",
        state=RecordingSessionState.COMPUTER_INGESTED,
        revision=1,
        status_code="backup_required",
        current_stage="ingest",
        progress=1.0,
        blocking_reason="backup_required",
        created_at=NOW,
        updated_at=NOW,
    )
    asset = AudioAsset(
        asset_id="asset-1",
        sha256="a" * 64,
        size_bytes=5,
        duration_ms=2000,
        format=AudioFormat.M4A,
        media_id="media-1",
        created_at=NOW,
    )
    replica = AudioReplica(
        replica_id="replica-1",
        asset_id=asset.asset_id,
        device_id=device.device_id,
        storage_key="aa/aa/" + "a" * 64,
        state=AudioReplicaState.AVAILABLE,
        verified_at=NOW,
        created_at=NOW,
    )
    segment = CaptureSegment(
        segment_id="segment-1",
        session_id=session.session_id,
        asset_id=asset.asset_id,
        replica_id=replica.replica_id,
        sequence=0,
        session_start_ms=0,
        session_end_ms=2000,
        source_start_ms=0,
        source_end_ms=2000,
        start_sample=0,
        captured_at=NOW,
    )
    return device, session, asset, replica, segment


if __name__ == "__main__":
    unittest.main()
