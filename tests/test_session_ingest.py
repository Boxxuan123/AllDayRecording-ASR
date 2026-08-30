from __future__ import annotations

import json
import sqlite3
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from allday_asr.audio.tools import AudioMetadata
from allday_asr.services.session_ingest import (
    CANONICAL_MANIFEST_FORMAT,
    ingest_session_manifest,
)
from allday_asr.storage.database import Database


class SessionManifestIngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parent / f"session-ingest-{uuid4().hex}"
        self.root.mkdir()
        self.database = Database.open(self.root / "state.sqlite3")

    def tearDown(self) -> None:
        for path in self.root.iterdir():
            path.unlink(missing_ok=True)
        self.root.rmdir()

    def test_identical_silent_chunks_remain_distinct_instances_and_import_is_idempotent(
        self,
    ) -> None:
        first = self.root / "first.wav"
        second = self.root / "second.wav"
        self._write_wav(first, frames=1_600)
        self._write_wav(second, frames=1_600)
        manifest = self._write_manifest(
            "stable-session",
            [
                self._chunk(10, first.name, 0, 1_600),
                self._chunk(20, second.name, 1_600, 1_600),
            ],
        )

        with patch(
            "allday_asr.services.session_ingest.probe_audio",
            side_effect=lambda path: self._metadata(path, duration_ms=100),
        ):
            first_result = ingest_session_manifest(self.database, manifest)
            second_result = ingest_session_manifest(self.database, manifest)

        self.assertTrue(first_result.created)
        self.assertFalse(second_result.created)
        self.assertEqual(first_result.session_id, second_result.session_id)
        self.assertEqual(first_result.chunk_count, 2)
        self.assertEqual(first_result.duration_ms, 200)
        self.assertEqual(first_result.gap_count, 0)
        self.assertEqual(first_result.overlap_count, 0)

        session = self.database.get_recording_session(first_result.session_id)
        self.assertEqual(session["status"], "closed")
        self.assertIsNone(session["legacy_recording_id"])
        sources = self.database.list_session_sources(first_result.session_id)
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0]["source_object_id"], sources[1]["source_object_id"])
        self.assertNotEqual(
            sources[0]["source_instance_id"], sources[1]["source_instance_id"]
        )
        self.assertEqual(len(self.database.list_source_objects()), 1)
        self.assertEqual(len(self.database.list_source_instances()), 2)
        self.assertIsNotNone(
            self.database.get_session_manifest(first_result.session_id)
        )

        run_id = self.database.start_processing_run(
            None,
            session_id=first_result.session_id,
            run_kind="quality_asr_v2c",
            config={"test": True},
            config_sha256="test-config",
        )
        run = self.database.get_processing_run(run_id)
        self.assertIsNone(run["recording_id"])
        self.assertEqual(run["session_id"], first_result.session_id)
        inputs = self.database.list_processing_run_inputs(run_id)
        self.assertEqual(len(inputs), 2)
        self.assertNotEqual(inputs[0]["source_instance_id"], inputs[1]["source_instance_id"])

        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE session_sources SET session_start_ms = 1 WHERE id = ?",
                    (sources[0]["id"],),
                )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.connect() as connection:
                connection.execute(
                    "DELETE FROM session_sources WHERE id = ?", (sources[0]["id"],)
                )

    def test_preflight_failure_leaves_no_partial_session(self) -> None:
        good = self.root / "good.wav"
        bad = self.root / "bad.wav"
        self._write_wav(good, frames=1_600)
        self._write_wav(bad, frames=800)
        manifest = self._write_manifest(
            "invalid-session",
            [
                self._chunk(1, good.name, 0, 1_600),
                self._chunk(2, bad.name, 1_600, 1_600),
            ],
        )
        with patch(
            "allday_asr.services.session_ingest.probe_audio",
            side_effect=lambda path: self._metadata(path, duration_ms=100),
        ):
            with self.assertRaisesRegex(ValueError, "frame 数"):
                ingest_session_manifest(self.database, manifest)
        self.assertIsNone(self.database.find_recording_session("invalid-session"))
        self.assertEqual(self.database.list_source_instances(), [])

    def test_manifest_detects_gap_and_rejects_false_continuity_claim(self) -> None:
        first = self.root / "a.wav"
        second = self.root / "b.wav"
        self._write_wav(first, frames=1_600, sample=1)
        self._write_wav(second, frames=1_600, sample=2)
        manifest = self._write_manifest(
            "gap-session",
            [
                self._chunk(1, first.name, 0, 1_600),
                self._chunk(2, second.name, 3_200, 1_600),
            ],
            continuity_valid=True,
        )
        with patch(
            "allday_asr.services.session_ingest.probe_audio",
            side_effect=lambda path: self._metadata(path, duration_ms=100),
        ):
            with self.assertRaisesRegex(ValueError, "清单声明连续"):
                ingest_session_manifest(self.database, manifest)
        self.assertIsNone(self.database.find_recording_session("gap-session"))

    def test_manifest_rejects_fractional_sample_coordinates(self) -> None:
        audio = self.root / "fractional.wav"
        self._write_wav(audio, frames=1_600)
        manifest = self._write_manifest(
            "fractional-session",
            [self._chunk(1, audio.name, 0, 1_600)],
        )
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["chunks"][0]["firstSample"] = 0.5
        manifest.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "必须是整数"):
            ingest_session_manifest(self.database, manifest)
        self.assertIsNone(self.database.find_recording_session("fractional-session"))

    def _write_manifest(
        self,
        session_key: str,
        chunks: list[dict[str, object]],
        *,
        continuity_valid: bool | None = None,
    ) -> Path:
        payload: dict[str, object] = {
            "format": CANONICAL_MANIFEST_FORMAT,
            "sessionKey": session_key,
            "sessionStartedAt": "2026-08-29T01:00:00+00:00",
            "device": "synthetic-watch",
            "timezone": "Asia/Singapore",
            "audio": {
                "sampleRate": 16_000,
                "channels": 1,
                "bitsPerSample": 16,
            },
            "chunks": chunks,
        }
        if continuity_valid is not None:
            payload["continuityValid"] = continuity_valid
        path = self.root / f"{session_key}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    @staticmethod
    def _chunk(
        index: int, file_name: str, first_sample: int, sample_count: int
    ) -> dict[str, object]:
        return {
            "index": index,
            "fileName": file_name,
            "firstSample": first_sample,
            "sampleCount": sample_count,
        }

    @staticmethod
    def _write_wav(path: Path, *, frames: int, sample: int = 0) -> None:
        with wave.open(str(path), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16_000)
            writer.writeframes(int(sample).to_bytes(2, "little", signed=True) * frames)

    @staticmethod
    def _metadata(path: Path, *, duration_ms: int) -> AudioMetadata:
        return AudioMetadata(
            duration_ms=duration_ms,
            codec="pcm_s16le",
            sample_rate=16_000,
            channels=1,
            bit_rate=256_000,
            recorded_at="2026-08-29T01:00:00+00:00",
            encoder=None,
        )


if __name__ == "__main__":
    unittest.main()
