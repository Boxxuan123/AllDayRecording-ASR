from __future__ import annotations

import hashlib
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from allday_asr.audio.tools import AudioMetadata
from allday_asr.services.sources import (
    audit_source_object,
    logical_window_cache_key,
    materialize_logical_window,
    plan_logical_windows,
)
from allday_asr.storage.database import Database


class SourceObjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parent
        self.token = uuid4().hex
        self.database_path = self.root / f"sources-{self.token}.sqlite3"
        self.database = Database.open(self.database_path)
        self.paths: list[Path] = []

    def tearDown(self) -> None:
        for path in self.paths:
            path.unlink(missing_ok=True)
        for suffix in ("", "-shm", "-wal"):
            Path(f"{self.database_path}{suffix}").unlink(missing_ok=True)

    def test_integrity_audit_detects_mutation_without_updating_expected_hash(self) -> None:
        path = self.root / f"watch-{self.token}.m4a"
        self.paths.append(path)
        original = b"permanent-original-audio"
        path.write_bytes(original)
        digest = hashlib.sha256(original).hexdigest()
        source = self.database.create_source_object(
            {
                "sha256": digest,
                "source_path": str(path.resolve()),
                "byte_size": len(original),
                "container": "m4a",
                "codec": "aac",
                "sample_rate": 16_000,
                "channels": 1,
                "bit_rate": 64_000,
                "recorded_at": "2026-08-28T00:00:00+00:00",
                "timezone": "Asia/Singapore",
                "duration_ms": 5_000,
                "device": "watch",
            }
        )
        metadata = AudioMetadata(
            duration_ms=5_000,
            codec="aac",
            sample_rate=16_000,
            channels=1,
            bit_rate=64_000,
            recorded_at="2026-08-28T00:00:00+00:00",
            encoder="test",
        )
        with patch("allday_asr.services.sources.probe_audio", return_value=metadata):
            first = audit_source_object(self.database, int(source["id"]))
        self.assertEqual(first.status, "verified")

        path.write_bytes(b"modified")
        with patch("allday_asr.services.sources.probe_audio", return_value=metadata):
            second = audit_source_object(self.database, int(source["id"]))
        self.assertEqual(second.status, "mismatch")
        self.assertIn("sha256", second.mismatches)
        persisted = self.database.get_source_object(int(source["id"]))
        self.assertEqual(persisted["sha256"], digest)
        self.assertEqual(persisted["integrity_status"], "mismatch")
        self.assertEqual(len(self.database.list_source_integrity_audits(source["id"])), 2)

    def test_cross_source_windows_are_complete_and_materialize_only_one_window(self) -> None:
        first_path = self.root / f"chunk-a-{self.token}.m4a"
        second_path = self.root / f"chunk-b-{self.token}.m4a"
        output_path = self.root / f"window-{self.token}.wav"
        self.paths.extend([first_path, second_path, output_path])
        first_path.write_bytes(b"a")
        second_path.write_bytes(b"b")
        first = self._source(first_path, duration_ms=1_000)
        second = self._source(second_path, duration_ms=1_000)
        session = self.database.create_recording_session(
            {
                "session_key": f"test:{self.token}",
                "device": "watch",
                "recorded_at": "2026-08-28T00:00:00+00:00",
                "timezone": "Asia/Singapore",
                "duration_ms": 0,
                "status": "active",
            }
        )
        self.database.add_session_source(
            {
                "session_id": session["id"],
                "source_object_id": first["id"],
                "chunk_index": 0,
                "session_start_ms": 0,
                "session_end_ms": 1_000,
                "source_end_ms": 1_000,
                "continuity_status": "continuous",
            }
        )
        self.database.add_session_source(
            {
                "session_id": session["id"],
                "source_object_id": second["id"],
                "chunk_index": 1,
                "session_start_ms": 1_000,
                "session_end_ms": 2_000,
                "source_end_ms": 1_000,
                "continuity_status": "continuous",
            }
        )

        self.assertEqual(
            self.database.get_recording_session(int(session["id"]))["duration_ms"],
            2_000,
        )

        windows = plan_logical_windows(
            self.database, int(session["id"]), window_ms=1_000, context_ms=100
        )
        self.assertEqual(len(windows), 2)
        self.assertTrue(windows[0].coverage_complete)
        self.assertEqual(len(windows[0].slices), 2)
        self.assertEqual(windows[0].analysis_end_ms, 1_100)
        self.assertEqual(windows[1].analysis_start_ms, 900)
        self.assertEqual(
            logical_window_cache_key(windows[0]), logical_window_cache_key(windows[0])
        )
        self.assertNotEqual(
            logical_window_cache_key(windows[0]), logical_window_cache_key(windows[1])
        )

        def fake_extract(source, destination, start_ms, end_ms, *, audio_filter=None):
            del source, audio_filter
            frames = round((end_ms - start_ms) * 16_000 / 1000)
            with wave.open(str(destination), "wb") as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(16_000)
                writer.writeframes(b"\x00\x00" * frames)
            return destination

        with patch("allday_asr.services.sources.extract_clip", side_effect=fake_extract):
            materialize_logical_window(windows[0], output_path)
        with wave.open(str(output_path), "rb") as reader:
            self.assertEqual(reader.getframerate(), 16_000)
            self.assertEqual(reader.getnframes(), 17_600)

    def _source(self, path: Path, *, duration_ms: int):
        payload = path.read_bytes()
        return self.database.create_source_object(
            {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "source_path": str(path.resolve()),
                "byte_size": len(payload),
                "container": "m4a",
                "codec": "aac",
                "sample_rate": 16_000,
                "channels": 1,
                "bit_rate": 64_000,
                "recorded_at": "2026-08-28T00:00:00+00:00",
                "timezone": "Asia/Singapore",
                "duration_ms": duration_ms,
                "device": "watch",
            }
        )


if __name__ == "__main__":
    unittest.main()
