from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from allday_asr.services.actions import extract_action_candidates
from allday_asr.storage.database import Database


class ActionCandidateTests(unittest.TestCase):
    def test_schedule_requires_and_preserves_self_confirmation_evidence(self) -> None:
        suffix = uuid4().hex
        database_path = Path(__file__).parent / f"actions-{suffix}.sqlite3"
        output_dir = Path(__file__).parent / f"actions-output-{suffix}"
        try:
            output_dir.mkdir()
            database = Database(database_path)
            recording = database.create_recording(
                {
                    "source_path": str(Path(__file__).parent / "audio.m4a"),
                    "sha256": f"actions-{suffix}",
                    "device": "test",
                    "recorded_at": "2026-08-24T12:00:00+00:00",
                    "timezone": "Asia/Singapore",
                    "duration_ms": 20_000,
                    "codec": "aac",
                    "sample_rate": 16_000,
                    "channels": 1,
                    "bit_rate": 64_000,
                    "encoder": "test",
                }
            )
            recording_id = int(recording["id"])
            database.replace_vad_segments(
                recording_id,
                [(0, 2_000), (3_000, 4_000)],
                str(Path(__file__).parent / "audio.m4a"),
            )
            segments = database.all_segments(recording_id)
            for segment, text in zip(
                segments, ["明天十点饭店见", "好的"], strict=True
            ):
                database.mark_segment_running(int(segment["id"]))
                database.mark_segment_completed(
                    int(segment["id"]),
                    language="zh",
                    text_raw=text,
                    text_display=text,
                    asr_model="test",
                )
            database.replace_speaker_labels(
                recording_id,
                [
                    (int(segments[0]["id"]), "speaker_00"),
                    (int(segments[1]["id"]), "speaker_01"),
                ],
            )
            profile = database.upsert_self_profile(
                display_name="我",
                embedding_model="test",
                embedding_version="1",
                embedding_path="voiceprint.npz",
            )
            database.assign_person_to_segments(
                recording_id, int(profile["id"]), [(int(segments[1]["id"]), 0.9)]
            )

            with patch(
                "allday_asr.services.actions.recording_output_dir",
                return_value=output_dir,
            ):
                first = extract_action_candidates(database, recording_id)
                second = extract_action_candidates(database, recording_id)

            self.assertEqual(first.detected_now, 1)
            self.assertEqual(second.total_candidates, 1)
            candidate = database.list_action_candidates(recording_id)[0]
            self.assertEqual(candidate["candidate_type"], "schedule")
            self.assertEqual(candidate["location"], "饭店")
            self.assertEqual(candidate["scheduled_at"], "2026-08-25T10:00:00+08:00")
            self.assertEqual(candidate["status"], "pending")

            database.review_action_candidate(int(candidate["id"]), status="confirmed")
            with patch(
                "allday_asr.services.actions.recording_output_dir",
                return_value=output_dir,
            ):
                extract_action_candidates(database, recording_id)
            self.assertEqual(
                database.get_action_candidate(int(candidate["id"]))["status"],
                "confirmed",
            )
        finally:
            if output_dir.exists():
                shutil.rmtree(output_dir)
            for database_suffix in ("", "-shm", "-wal"):
                candidate_path = Path(f"{database_path}{database_suffix}")
                if candidate_path.exists():
                    candidate_path.unlink()


if __name__ == "__main__":
    unittest.main()
