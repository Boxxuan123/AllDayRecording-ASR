from __future__ import annotations

import json
import unittest
from pathlib import Path
from uuid import uuid4

from allday_asr.exporters import _absolute_timestamp, _format_offset, _group_segments
from allday_asr.asr.funasr_backend import ASR_MODEL_ID, _cached_model_or_id
from allday_asr.services.diarization import (
    audit_speaker_assignments,
    filter_speaker_assignments,
    match_speaker_turns,
)
from allday_asr.storage.database import Database


class ExporterTests(unittest.TestCase):
    def test_format_offset(self) -> None:
        self.assertEqual(_format_offset(3_723_000), "01:02:03")

    def test_absolute_timestamp_uses_recording_timezone(self) -> None:
        value = _absolute_timestamp(
            "2026-08-24T12:23:54+00:00", 60_000, "Asia/Singapore"
        )
        self.assertEqual(value, "2026-08-24T20:24:54+08:00")

    def test_group_segments(self) -> None:
        segments = [
            {"start_ms": 0, "end_ms": 1_000},
            {"start_ms": 5_000, "end_ms": 7_000},
            {"start_ms": 200_000, "end_ms": 201_000},
        ]
        groups = _group_segments(segments, max_gap_ms=120_000)
        self.assertEqual([len(group) for group in groups], [2, 1])

    def test_cached_model_resolution_returns_a_valid_source(self) -> None:
        source = _cached_model_or_id(ASR_MODEL_ID)
        self.assertTrue(source == ASR_MODEL_ID or Path(source).is_dir())

    def test_match_speaker_turns_uses_overlap(self) -> None:
        segments = [
            {"id": 10, "start_ms": 100, "end_ms": 900},
            {"id": 11, "start_ms": 1_000, "end_ms": 2_000},
        ]
        turns = [
            {"start": 0, "end": 950, "spk": 1},
            {"start": 980, "end": 2_100, "spk": 0},
        ]
        self.assertEqual(
            match_speaker_turns(segments, turns),
            [(10, "speaker_01"), (11, "speaker_00")],
        )

    def test_filter_speaker_assignments_rejects_short_and_weak_clusters(self) -> None:
        segments = [
            {"id": 1, "start_ms": 0, "end_ms": 2_000},
            {"id": 2, "start_ms": 3_000, "end_ms": 5_000},
            {"id": 3, "start_ms": 6_000, "end_ms": 8_000},
            {"id": 4, "start_ms": 9_000, "end_ms": 9_900},
            {"id": 5, "start_ms": 10_000, "end_ms": 12_000},
        ]
        filtered, stats = filter_speaker_assignments(
            segments,
            [
                (1, "speaker_00"),
                (2, "speaker_00"),
                (3, "speaker_00"),
                (4, "speaker_00"),
                (5, "speaker_01"),
            ],
        )
        self.assertEqual(
            filtered,
            [
                (1, "speaker_00"),
                (2, "speaker_00"),
                (3, "speaker_00"),
                (4, None),
                (5, None),
            ],
        )
        self.assertEqual(stats, {"rejected_short": 1, "rejected_weak_cluster": 1})

    def test_speaker_audit_refreshes_stage_details(self) -> None:
        database_path = Path(__file__).parent / f"test-{uuid4().hex}.sqlite3"
        try:
            database = Database.open(database_path)
            recording = database.create_recording(
                {
                    "source_path": str(Path(__file__).parent / "audio.m4a"),
                    "sha256": "speaker-audit",
                    "device": "test",
                    "recorded_at": "2026-08-24T12:00:00+00:00",
                    "timezone": "Asia/Singapore",
                    "duration_ms": 10_000,
                    "codec": "aac",
                    "sample_rate": 16_000,
                    "channels": 1,
                    "bit_rate": 64_000,
                    "encoder": "test",
                }
            )
            database.replace_vad_segments(
                int(recording["id"]),
                [(0, 2_000), (3_000, 5_000), (6_000, 8_000), (9_000, 9_900)],
                str(Path(__file__).parent / "audio.m4a"),
            )
            segments = database.all_segments(int(recording["id"]))
            database.replace_speaker_labels(
                int(recording["id"]),
                [(int(segment["id"]), "speaker_00") for segment in segments],
            )
            database.set_stage(
                int(recording["id"]),
                "diarization",
                "completed",
                model_id="test-model",
                model_version="1",
                details={"assigned_segments": 4, "centers_path": "centers.npz"},
            )

            summary = audit_speaker_assignments(database, int(recording["id"]))

            self.assertEqual(summary.kept_segments, 3)
            stage = database.get_stage(int(recording["id"]), "diarization")
            details = json.loads(stage["details_json"])
            self.assertEqual(details["assigned_segments"], 3)
            self.assertEqual(details["unassigned_segments"], 1)
            self.assertTrue(details["quality_audit_applied"])
            self.assertEqual(details["centers_path"], "centers.npz")
        finally:
            for suffix in ("", "-shm", "-wal"):
                candidate = Path(f"{database_path}{suffix}")
                if candidate.exists():
                    candidate.unlink()


if __name__ == "__main__":
    unittest.main()
