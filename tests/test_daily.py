from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from allday_asr.config import AppConfig
from allday_asr.services.daily import run_daily
from allday_asr.storage.database import Database


class DailyRunTests(unittest.TestCase):
    def test_completed_human_review_is_reused_without_model_calls(self) -> None:
        suffix = uuid4().hex
        database_path = Path(__file__).parent / f"daily-{suffix}.sqlite3"
        normalized_path = Path(__file__).parent / f"normalized-{suffix}.wav"
        output_dir = Path(__file__).parent / f"daily-output-{suffix}"
        try:
            normalized_path.write_bytes(b"existing normalized audio placeholder")
            output_dir.mkdir()
            database = Database.open(database_path)
            recording = database.create_recording(
                {
                    "source_path": str(Path(__file__).parent / "audio.m4a"),
                    "sha256": f"daily-{suffix}",
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
            recording_id = int(recording["id"])
            database.update_recording(
                recording_id, normalized_path=str(normalized_path), status="completed"
            )
            database.replace_vad_segments(
                recording_id, [(0, 2_000)], str(Path(__file__).parent / "audio.m4a")
            )
            segment = database.all_segments(recording_id)[0]
            database.mark_segment_running(int(segment["id"]))
            database.mark_segment_completed(
                int(segment["id"]),
                language="zh",
                text_raw="测试",
                text_display="测试",
                asr_model="test",
            )
            database.set_stage(recording_id, "asr", "completed")
            database.set_stage(recording_id, "diarization", "completed")
            profile = database.upsert_self_profile(
                display_name="我",
                embedding_model="test",
                embedding_version="1",
                embedding_path="voiceprint.npz",
            )
            database.upsert_segment_annotations(
                recording_id,
                [
                    {
                        "segment_id": int(segment["id"]),
                        "identity_label": "self",
                        "raw_label": "我",
                        "confidence": "confirmed",
                    }
                ],
            )
            database.assign_person_to_segments(
                recording_id, int(profile["id"]), [(int(segment["id"]), 0.9)]
            )

            timeline = SimpleNamespace(
                event_count=1,
                segment_count=1,
                markdown_path=output_dir / "timeline.md",
                json_path=output_dir / "timeline.json",
            )
            actions = SimpleNamespace(
                detected_now=0,
                total_candidates=0,
                pending_candidates=0,
                markdown_path=output_dir / "action-candidates.md",
                json_path=output_dir / "action-candidates.json",
            )
            with (
                patch(
                    "allday_asr.services.daily.process_recording",
                    side_effect=AssertionError("ASR must be reused"),
                ),
                patch(
                    "allday_asr.services.daily.diarize_recording",
                    side_effect=AssertionError("diarization must be reused"),
                ),
                patch(
                    "allday_asr.services.daily.export_self_candidates",
                    side_effect=AssertionError("human review must be reused"),
                ),
                patch("allday_asr.services.daily.build_timeline", return_value=timeline),
                patch(
                    "allday_asr.services.daily.extract_action_candidates",
                    return_value=actions,
                ),
                patch(
                    "allday_asr.services.daily.export_jsonl",
                    side_effect=lambda _db, _id, destination: destination,
                ),
                patch(
                    "allday_asr.services.daily.export_markdown",
                    side_effect=lambda _db, _id, destination, **_kwargs: destination,
                ),
                patch(
                    "allday_asr.services.daily.recording_output_dir",
                    return_value=output_dir,
                ),
            ):
                first = run_daily(database, recording_id, AppConfig())
                second = run_daily(database, recording_id, AppConfig())

            self.assertEqual(first.status, "completed")
            self.assertEqual(second.status, "completed")
            self.assertFalse(first.review_actions)
            self.assertEqual(len(database.list_processing_runs(recording_id)), 2)
            self.assertTrue(first.manifest_json_path.is_file())
            self.assertIn("复用已有标签", first.manifest_markdown_path.read_text("utf-8"))
            self.assertIn("**actions**", first.manifest_markdown_path.read_text("utf-8"))
        finally:
            if output_dir.exists():
                shutil.rmtree(output_dir)
            if normalized_path.exists():
                normalized_path.unlink()
            for database_suffix in ("", "-shm", "-wal"):
                candidate = Path(f"{database_path}{database_suffix}")
                if candidate.exists():
                    candidate.unlink()


if __name__ == "__main__":
    unittest.main()
