from __future__ import annotations

import json
import shutil
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from allday_asr.audio.tools import AudioMetadata, sha256_file
from allday_asr.services.session_backup import (
    create_session_backup,
    verify_session_backup,
)
from allday_asr.services.session_ingest import (
    CANONICAL_MANIFEST_FORMAT,
    ingest_session_manifest,
)
from allday_asr.services.session_readiness import evaluate_session_readiness
from allday_asr.storage.database import Database


class SessionBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (Path(__file__).parent / f"session-backup-{uuid4().hex}").resolve()
        self.source_root = self.root / "source"
        self.backup_root = self.root / "backup-device"
        self.restore_root = self.root / "restore-probes"
        self.source_root.mkdir(parents=True)
        self.database = Database.open(self.root / "state.sqlite3")

    def tearDown(self) -> None:
        tests_root = Path(__file__).parent.resolve()
        self.root.relative_to(tests_root)
        shutil.rmtree(self.root)

    def test_independent_backup_is_complete_idempotent_and_production_ready(self) -> None:
        session_id = self._create_session()
        originals = {
            path: (path.stat().st_size, sha256_file(path))
            for path in self.source_root.iterdir()
        }

        first = create_session_backup(
            self.database,
            session_id,
            self.backup_root,
            storage_kind="independent_device",
            restore_drill=True,
            restore_probe_root=self.restore_root,
        )
        second = create_session_backup(
            self.database,
            session_id,
            self.backup_root,
            storage_kind="independent_device",
            restore_drill=True,
            restore_probe_root=self.restore_root,
        )

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.backup_id, second.backup_id)
        self.assertEqual(first.file_count, 3)
        self.assertTrue(first.restore_verified)
        self.assertEqual(len(self.database.list_session_backups(session_id)), 1)
        self.assertTrue((first.backup_path / "backup-manifest.json").is_file())
        self.assertEqual(list(self.restore_root.iterdir()), [])
        for path, expected in originals.items():
            self.assertEqual((path.stat().st_size, sha256_file(path)), expected)
        source_rows = self.database.list_session_sources(session_id)
        instance_rows = {
            int(row["id"]): row for row in self.database.list_source_instances()
        }
        self.assertTrue(
            all(
                instance_rows[int(row["source_instance_id"])]["backup_status"]
                == "verified"
                for row in source_rows
            )
        )

        readiness = evaluate_session_readiness(self.database, session_id)
        self.assertEqual(readiness["state"], "production_ready")
        self.assertTrue(readiness["production_ready"])
        self.assertEqual(readiness["production_backup_id"], first.backup_id)

    def test_same_device_copy_only_allows_shadow_workflow(self) -> None:
        session_id = self._create_session()
        summary = create_session_backup(
            self.database,
            session_id,
            self.backup_root,
            storage_kind="same_device_test",
            restore_drill=True,
            restore_probe_root=self.restore_root,
        )

        self.assertTrue(summary.restore_verified)
        readiness = evaluate_session_readiness(self.database, session_id)
        self.assertEqual(readiness["state"], "shadow_ready")
        self.assertTrue(readiness["shadow_ready"])
        self.assertFalse(readiness["production_ready"])
        self.assertIn("独立设备/网络备份", readiness["production_blockers"][0])
        self.assertTrue(
            all(
                row["backup_status"] == "pending"
                for row in self.database.list_source_instances()
            )
        )

    def test_tampered_backup_is_failed_and_loses_restore_qualification(self) -> None:
        session_id = self._create_session()
        summary = create_session_backup(
            self.database,
            session_id,
            self.backup_root,
            storage_kind="network",
            restore_drill=True,
            restore_probe_root=self.restore_root,
        )
        evidence = self.database.list_session_backup_files(summary.backup_id)
        target = summary.backup_path / str(evidence[0]["relative_path"])
        target.write_bytes(b"synthetic-tamper")

        with self.assertRaisesRegex(RuntimeError, "大小不一致|SHA-256"):
            verify_session_backup(self.database, summary.backup_id)

        record = self.database.get_session_backup(summary.backup_id)
        self.assertEqual(record["status"], "failed")
        self.assertIsNone(record["restore_verified_at"])
        readiness = evaluate_session_readiness(self.database, session_id)
        self.assertEqual(readiness["state"], "shadow_ready")
        self.assertIsNone(readiness["production_backup_id"])

    def test_unexpected_file_makes_backup_non_deterministic_and_invalid(self) -> None:
        session_id = self._create_session()
        summary = create_session_backup(
            self.database,
            session_id,
            self.backup_root,
            storage_kind="network",
            restore_drill=False,
        )
        (summary.backup_path / "not-in-manifest.txt").write_text(
            "synthetic extra", encoding="utf-8"
        )

        with self.assertRaisesRegex(RuntimeError, "清单外文件"):
            verify_session_backup(self.database, summary.backup_id)
        self.assertEqual(
            self.database.get_session_backup(summary.backup_id)["status"], "failed"
        )

    def test_changed_original_blocks_backup_before_copy(self) -> None:
        session_id = self._create_session()
        source = self.source_root / "first.wav"
        source.write_bytes(source.read_bytes() + b"changed")

        with self.assertRaisesRegex(RuntimeError, "原始输入完整性校验失败"):
            create_session_backup(
                self.database,
                session_id,
                self.backup_root,
                storage_kind="independent_device",
            )

        self.assertEqual(self.database.list_session_backups(session_id), [])
        self.assertFalse(self.backup_root.exists())

    def test_database_rejects_evidence_that_does_not_match_immutable_input(self) -> None:
        session_id = self._create_session()
        sources = self.database.list_session_sources(session_id)
        manifest = self.database.get_session_manifest(session_id)
        assert manifest is not None
        files = [
            {
                "position": index,
                "file_kind": "source_audio",
                "source_instance_id": int(row["source_instance_id"]),
                "relative_path": f"audio/{index}.wav",
                "sha256": "0" * 64 if index == 0 else str(row["sha256"]),
                "byte_size": int(row["instance_byte_size"]),
            }
            for index, row in enumerate(sources)
        ]
        files.append(
            {
                "position": len(files),
                "file_kind": "capture_manifest",
                "source_instance_id": None,
                "relative_path": "capture/manifest.json",
                "sha256": str(manifest["manifest_sha256"]),
                "byte_size": int(manifest["byte_size"]),
            }
        )
        values = {
            "backup_key": "untrusted-evidence",
            "session_id": session_id,
            "storage_kind": "network",
            "backup_path": str(self.backup_root / "untrusted"),
            "input_fingerprint": self.database.session_input_fingerprint(session_id),
            "backup_manifest_sha256": "1" * 64,
        }
        with self.assertRaisesRegex(ValueError, "输入指纹"):
            self.database.create_verified_session_backup(
                {**values, "input_fingerprint": "wrong"}, files
            )
        with self.assertRaisesRegex(ValueError, "不可变原始输入不一致"):
            self.database.create_verified_session_backup(values, files)

    def test_destination_beside_originals_is_rejected(self) -> None:
        session_id = self._create_session()
        with self.assertRaisesRegex(ValueError, "不能位于任一原音所在目录内"):
            create_session_backup(
                self.database,
                session_id,
                self.source_root / "not-independent",
                storage_kind="independent_device",
            )

    def _create_session(self) -> int:
        first = self.source_root / "first.wav"
        second = self.source_root / "second.wav"
        self._write_wav(first, frames=1_600)
        self._write_wav(second, frames=1_600)
        manifest = self.source_root / "capture.json"
        manifest.write_text(
            json.dumps(
                {
                    "format": CANONICAL_MANIFEST_FORMAT,
                    "sessionKey": f"backup-test-{uuid4().hex}",
                    "sessionStartedAt": "2026-08-29T01:00:00+00:00",
                    "device": "synthetic-watch",
                    "timezone": "Asia/Singapore",
                    "audio": {
                        "sampleRate": 16_000,
                        "channels": 1,
                        "bitsPerSample": 16,
                    },
                    "chunks": [
                        {
                            "index": 10,
                            "fileName": first.name,
                            "firstSample": 0,
                            "sampleCount": 1_600,
                        },
                        {
                            "index": 20,
                            "fileName": second.name,
                            "firstSample": 1_600,
                            "sampleCount": 1_600,
                        },
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        with patch(
            "allday_asr.services.session_ingest.probe_audio",
            side_effect=lambda path: self._metadata(path),
        ):
            return ingest_session_manifest(self.database, manifest).session_id

    @staticmethod
    def _write_wav(path: Path, *, frames: int) -> None:
        with wave.open(str(path), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16_000)
            writer.writeframes(b"\x00\x00" * frames)

    @staticmethod
    def _metadata(path: Path) -> AudioMetadata:
        return AudioMetadata(
            duration_ms=100,
            codec="pcm_s16le",
            sample_rate=16_000,
            channels=1,
            bit_rate=256_000,
            recorded_at="2026-08-29T01:00:00+00:00",
            encoder=None,
        )


if __name__ == "__main__":
    unittest.main()
