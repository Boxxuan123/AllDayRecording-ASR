from __future__ import annotations

import json
import shutil
import sqlite3
import unittest
import wave
from pathlib import Path
from uuid import uuid4

import numpy as np

from allday_asr.v3.adapters.identity_calibration import run_identity_calibration
from allday_asr.v3.domain.identity import IdentityAcceptancePolicy


class _FixtureEmbeddingBackend:
    def extract_speaker_embeddings(
        self, samples: list[np.ndarray], *, batch_size: int = 16
    ) -> np.ndarray:
        if batch_size != 16 or [len(value) for value in samples] != [48_000, 48_000]:
            raise AssertionError("calibration adapter did not read two complete windows")
        return np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)


class V31IdentityCalibrationAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parent / f"v31-calibration-{uuid4().hex}"
        self.root.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_read_only_database_audio_and_embedding_adapter_publish_receipts(
        self,
    ) -> None:
        with self.subTest(root=self.root):
            root = self.root
            database = root / "source.sqlite3"
            bundle = root / "bundle.json"
            progress = root / "progress.json"
            voiceprint = root / "self-voiceprint.npz"
            output = root / "identity"

            self_audio = root / "self.wav"
            other_audio = root / "other.wav"
            self._write_wav(self_audio, 1_000)
            self._write_wav(other_audio, -1_000)
            np.savez_compressed(
                voiceprint,
                embeddings=np.asarray([[1.0, 0.0]], dtype=np.float32),
                centroid=np.asarray([1.0, 0.0], dtype=np.float32),
            )
            self._database(database, self_audio, other_audio)
            bundle.write_text(
                json.dumps(
                    {
                        "bundle_sha256": "a" * 64,
                        "sessions": [
                            {"session_id": 1, "session_key": "recording:1"},
                            {"session_id": 2, "session_key": "recording:2"},
                        ],
                        "effective_identity_windows": [
                            {
                                "window_id": "self-holdout",
                                "session_id": 1,
                                "start_ms": 0,
                                "end_ms": 3_000,
                                "identity": "self",
                                "provenance_record_ids": ["label:1"],
                            },
                            {
                                "window_id": "other-holdout",
                                "session_id": 2,
                                "start_ms": 0,
                                "end_ms": 3_000,
                                "identity": "not_self",
                                "provenance_record_ids": ["label:2"],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            progress.write_text(
                json.dumps(
                    {
                        "updated_at": "2026-09-01T00:00:00Z",
                        "identity_candidates": [],
                        "identity_annotations": {},
                    }
                ),
                encoding="utf-8",
            )

            summary = run_identity_calibration(
                bundle,
                progress,
                database,
                output_dir=output,
                acceptance=IdentityAcceptancePolicy(
                    min_positive_holdout=1,
                    min_negative_holdout=1,
                    max_false_accept_rate=0.0,
                    max_false_reject_rate=0.0,
                ),
                voiceprint_path_override=voiceprint,
                backend_factory=_FixtureEmbeddingBackend,
            )

            self.assertTrue(summary.accepted)
            self.assertEqual(summary.blockers, ())
            self.assertEqual(summary.metrics.false_accept_rate, 0.0)
            self.assertEqual(summary.metrics.false_reject_rate, 0.0)
            for path in (
                summary.calibration_path,
                summary.policy_path,
                summary.receipt_path,
            ):
                self.assertTrue(path.is_file(), path)
            calibration = json.loads(
                summary.calibration_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                calibration["format"],
                "AllDayRecording V3.1 identity calibration v1",
            )
            self.assertEqual(calibration["source"]["database"], str(database))
            with sqlite3.connect(database) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM recordings").fetchone()[0],
                    2,
                )

    @staticmethod
    def _database(database: Path, self_audio: Path, other_audio: Path) -> None:
        with sqlite3.connect(database) as connection:
            connection.executescript(
                """
                CREATE TABLE recordings (
                    id INTEGER PRIMARY KEY,
                    normalized_path TEXT NOT NULL
                );
                CREATE TABLE recording_sessions (
                    id INTEGER PRIMARY KEY,
                    legacy_recording_id INTEGER NOT NULL
                );
                """
            )
            connection.executemany(
                "INSERT INTO recordings (id, normalized_path) VALUES (?, ?)",
                [(1, str(self_audio)), (2, str(other_audio))],
            )
            connection.executemany(
                "INSERT INTO recording_sessions (id, legacy_recording_id) VALUES (?, ?)",
                [(1, 1), (2, 2)],
            )

    @staticmethod
    def _write_wav(path: Path, sample: int) -> None:
        samples = np.full(48_000, sample, dtype=np.int16)
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16_000)
            audio.writeframes(samples.tobytes())


if __name__ == "__main__":
    unittest.main()
