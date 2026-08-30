from __future__ import annotations

import hashlib
import os
import sqlite3
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from allday_asr.diarization.quality_backends import (
    PyannoteCommunityBackend,
    QualityDiarizationResult,
    SpeakerTurn,
)
from allday_asr.services.quality_diarization import (
    QualityDiarizationSettings,
    compute_overlap_regions,
    run_quality_diarization,
    snapshot_quality_diarization,
)
from allday_asr.storage.database import Database


class FakeDiarizationBackend:
    model_id = "fake/community-1"
    model_revision = "test-revision"
    backend_name = "fake-overlap-aware"

    def __init__(self) -> None:
        self.loaded = False
        self.closed = False

    def ensure_loaded(self) -> None:
        self.loaded = True

    def diarize(self, audio_path: Path, **kwargs):
        del audio_path, kwargs
        return QualityDiarizationResult(
            regular_turns=(
                SpeakerTurn(100, 900, "SPEAKER_00"),
                SpeakerTurn(600, 1_200, "SPEAKER_01"),
                SpeakerTurn(1_200, 1_800, "SPEAKER_00"),
            ),
            exclusive_turns=(
                SpeakerTurn(100, 700, "SPEAKER_00"),
                SpeakerTurn(700, 1_200, "SPEAKER_01"),
                SpeakerTurn(1_200, 1_800, "SPEAKER_00"),
            ),
            raw_response={"fake": True},
        )

    def parameters(self):
        return {"fake": True}

    def close(self) -> None:
        self.closed = True


class QualityDiarizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parent
        self.token = uuid4().hex
        self.database_path = self.root / f"diarization-{self.token}.sqlite3"
        self.source_path = self.root / f"diarization-{self.token}.m4a"
        self.output_dir = self.root / f"diarization-output-{self.token}"
        self.source_bytes = b"immutable-watch-audio"
        self.source_path.write_bytes(self.source_bytes)
        self.database = Database.open(self.database_path)
        recording = self.database.create_recording(
            {
                "source_path": str(self.source_path.resolve()),
                "sha256": hashlib.sha256(self.source_bytes).hexdigest(),
                "device": "watch",
                "recorded_at": "2026-08-28T00:00:00+00:00",
                "timezone": "Asia/Singapore",
                "duration_ms": 2_000,
                "codec": "aac",
                "sample_rate": 16_000,
                "channels": 1,
                "bit_rate": 64_000,
                "encoder": "test",
            }
        )
        self.recording_id = int(recording["id"])
        self.session = self.database.get_session_for_recording(self.recording_id)
        self.source = self.database.list_session_sources(int(self.session["id"]))[0]
        self.asr_run_id = self._create_asr_run()

    def tearDown(self) -> None:
        for path in self.output_dir.glob("**/*") if self.output_dir.exists() else []:
            if path.is_file():
                path.unlink()
        for path in sorted(
            self.output_dir.glob("**/*") if self.output_dir.exists() else [], reverse=True
        ):
            if path.is_dir():
                path.rmdir()
        if self.output_dir.exists():
            self.output_dir.rmdir()
        self.source_path.unlink(missing_ok=True)
        for suffix in ("", "-shm", "-wal"):
            Path(f"{self.database_path}{suffix}").unlink(missing_ok=True)
        for backup in self.root.glob(f"diarization-{self.token}.schema-*.sqlite3"):
            backup.unlink(missing_ok=True)

    def test_overlap_timeline_token_fusion_snapshot_and_immutability(self) -> None:
        self.database.replace_vad_segments(
            self.recording_id, [(50, 1_900)], str(self.source_path.resolve())
        )
        segment = self.database.pending_segments(self.recording_id)[0]
        self.database.replace_speaker_labels(
            self.recording_id, [(int(segment["id"]), "legacy_speaker")]
        )

        @contextmanager
        def fake_window(window):
            self.assertEqual(window.analysis_start_ms, 0)
            self.assertEqual(window.analysis_end_ms, 2_000)
            yield self.source_path

        backend = FakeDiarizationBackend()
        with (
            patch(
                "allday_asr.services.quality_diarization.temporary_logical_window",
                fake_window,
            ),
            patch(
                "allday_asr.services.quality_diarization.OUTPUT_DIR", self.output_dir
            ),
        ):
            summary = run_quality_diarization(
                self.database,
                self.recording_id,
                asr_run_id=self.asr_run_id,
                settings=QualityDiarizationSettings(
                    min_secondary_overlap_ratio=0.25
                ),
                backend_factory=lambda: backend,
            )

        self.assertTrue(backend.loaded)
        self.assertTrue(backend.closed)
        self.assertEqual(summary.regular_turns, 3)
        self.assertEqual(summary.exclusive_turns, 3)
        self.assertEqual(summary.speakers, 2)
        self.assertEqual(summary.overlap_regions, 1)
        self.assertEqual(summary.overlap_ms, 300)
        self.assertEqual(self.source_path.read_bytes(), self.source_bytes)
        self.assertEqual(
            self.database.all_segments(self.recording_id)[0]["speaker_session_id"],
            "legacy_speaker",
        )

        turns = self.database.list_diarization_turns(summary.run_id)
        self.assertEqual(len(turns), 6)
        for turn in turns:
            source_rows = self.database.list_diarization_turn_sources(int(turn["id"]))
            self.assertEqual(len(source_rows), 1)
            self.assertEqual(
                sum(row["source_end_ms"] - row["source_start_ms"] for row in source_rows),
                turn["session_end_ms"] - turn["session_start_ms"],
            )

        tokens = self.database.list_committed_asr_tokens(self.asr_run_id)
        first = self.database.list_token_speaker_attributions(
            summary.run_id, token_id=int(tokens[0]["id"])
        )
        second = self.database.list_token_speaker_attributions(
            summary.run_id, token_id=int(tokens[1]["id"])
        )
        third = self.database.list_token_speaker_attributions(
            summary.run_id, token_id=int(tokens[2]["id"])
        )
        self.assertEqual(
            [(row["speaker_label"], row["attribution_kind"]) for row in first],
            [("SPEAKER_00", "primary")],
        )
        self.assertEqual(
            [(row["speaker_label"], row["attribution_kind"]) for row in second],
            [("SPEAKER_01", "primary"), ("SPEAKER_00", "overlap")],
        )
        self.assertEqual(
            [(row["speaker_label"], row["attribution_kind"]) for row in third],
            [(None, "none")],
        )

        snapshot = snapshot_quality_diarization(self.database, summary.run_id)
        predictions = self.database.list_benchmark_predictions(
            snapshot.prediction_set_id
        )
        self.assertEqual(snapshot.speaker_predictions, 3)
        self.assertEqual(snapshot.overlap_predictions, 1)
        self.assertEqual(len(predictions), 4)
        self.assertEqual(
            [
                (row["session_start_ms"], row["session_end_ms"])
                for row in predictions
                if row["prediction_kind"] == "overlap"
            ],
            [(600, 900)],
        )

        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE diarization_turns SET speaker_label = 'changed' WHERE id = ?",
                    (turns[0]["id"],),
                )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.connect() as connection:
                connection.execute(
                    "DELETE FROM token_speaker_attributions WHERE run_id = ?",
                    (summary.run_id,),
                )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "sealed"):
            with self.database.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO diarization_turns (
                        turn_key, run_id, session_id, turn_index, turn_kind,
                        speaker_label, session_start_ms, session_end_ms,
                        metadata_json, content_sha256, created_at
                    ) VALUES (?, ?, ?, 99, 'regular', 'late', 0, 10, '{}', 'x', 'now')
                    """,
                    (
                        f"late:{self.token}",
                        summary.run_id,
                        int(self.session["id"]),
                    ),
                )

    def test_overlap_regions_require_simultaneous_distinct_speakers(self) -> None:
        self.assertEqual(
            compute_overlap_regions(
                [
                    SpeakerTurn(0, 500, "A"),
                    SpeakerTurn(500, 1_000, "B"),
                ]
            ),
            [],
        )
        self.assertEqual(
            compute_overlap_regions(
                [
                    SpeakerTurn(0, 700, "A"),
                    SpeakerTurn(500, 1_000, "B"),
                ]
            ),
            [(500, 700, ("A", "B"))],
        )

    def test_gated_backend_fails_before_network_when_token_is_missing(self) -> None:
        with (
            patch.dict(os.environ, {"V2D_TEST_HF_TOKEN": ""}, clear=False),
            patch("huggingface_hub.get_token", return_value=None),
        ):
            backend = PyannoteCommunityBackend(token_env="V2D_TEST_HF_TOKEN")
            with (
                patch.object(
                    backend, "_resolve_model_source", return_value=backend.model_id
                ),
                self.assertRaisesRegex(RuntimeError, "尚未缓存"),
            ):
                backend.ensure_loaded()

    def _create_asr_run(self) -> int:
        run_id = self.database.start_processing_run(
            self.recording_id,
            run_kind="quality_asr_v2c",
            config={"test": True},
            config_sha256="test-asr-config",
            model_manifest={"primary": {"model_id": "fake/qwen"}},
            pipeline_version="v2-c",
        )
        source_common = {
            "source_object_id": int(self.source["source_object_id"]),
            "source_sha256": str(self.source["sha256"]),
        }
        tokens = []
        for index, (text, start_ms, end_ms) in enumerate(
            (("甲", 200, 400), ("乙", 650, 850), ("丙", 1_850, 1_950))
        ):
            tokens.append(
                {
                    "token_index": index,
                    "text": text,
                    "session_start_ms": start_ms,
                    "session_end_ms": end_ms,
                    "analysis_start_ms": start_ms,
                    "analysis_end_ms": end_ms,
                    "kept_in_core": True,
                    "alignment_model_id": "fake/aligner",
                    "source_refs": [
                        {
                            **source_common,
                            "source_start_ms": start_ms,
                            "source_end_ms": end_ms,
                        }
                    ],
                }
            )
        self.database.create_asr_hypothesis(
            {
                "hypothesis_key": f"test:{self.token}:primary",
                "run_id": run_id,
                "session_id": int(self.session["id"]),
                "window_index": 0,
                "hypothesis_role": "primary",
                "core_start_ms": 0,
                "core_end_ms": 2_000,
                "analysis_start_ms": 0,
                "analysis_end_ms": 2_000,
                "model_id": "fake/qwen",
                "backend": "fake",
                "text": "甲乙丙",
            },
            tokens,
        )
        self.database.finish_processing_run(
            run_id, status="completed", summary={"tokens": 3}
        )
        return run_id


if __name__ == "__main__":
    unittest.main()
