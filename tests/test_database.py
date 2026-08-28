from __future__ import annotations

import unittest
from pathlib import Path
from uuid import uuid4

from allday_asr.storage.database import Database


class DatabaseTests(unittest.TestCase):
    def test_recording_dedup_and_resumable_segments(self) -> None:
        database_path = Path(__file__).parent / f"test-{uuid4().hex}.sqlite3"
        try:
            database = Database(database_path)
            self.assertEqual(database.schema_version(), 3)
            values = {
                "source_path": str(Path(__file__).parent / "audio.m4a"),
                "sha256": "abc123",
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
            recording = database.create_recording(values)
            self.assertEqual(recording["id"], 1)
            self.assertEqual(database.find_recording_by_hash("abc123")["id"], 1)

            database.replace_vad_segments(1, [(100, 900), (1_200, 2_000)], values["source_path"])
            self.assertEqual(database.segment_count(1), 2)
            first = database.pending_segments(1, limit=1)[0]
            database.mark_segment_running(first["id"])
            self.assertEqual(database.reset_interrupted_segments(1), 1)
            first = database.pending_segments(1, limit=1)[0]
            database.mark_segment_running(first["id"])
            database.mark_segment_completed(
                first["id"],
                language="zh",
                text_raw="测试",
                text_display="测试",
                asr_model="test-model",
            )
            self.assertEqual(database.segment_status_counts(1), {"completed": 1, "pending": 1})

            profile = database.upsert_self_profile(
                display_name="我",
                embedding_model="test-speaker-model",
                embedding_version="1",
                embedding_path="voiceprint.npz",
            )
            database.replace_speaker_labels(1, [(first["id"], "speaker_00")])
            self.assertEqual(
                database.assign_person_to_speaker(1, "speaker_00", profile["id"]), 1
            )
            self.assertEqual(database.person_assignment_count(profile["id"]), 1)
            self.assertEqual(database.clear_person_assignments(1, profile["id"]), 1)
            self.assertEqual(database.person_assignment_count(profile["id"]), 0)
            run_id = database.start_processing_run(
                1,
                run_kind="daily",
                config={"version": 1},
                config_sha256="config-hash",
            )
            database.finish_processing_run(
                run_id,
                status="completed",
                summary={"segments": 2},
                artifacts={"timeline": "timeline.md"},
            )
            run = database.list_processing_runs(1)[0]
            self.assertEqual(run["status"], "completed")
            self.assertEqual(run["config_sha256"], "config-hash")
            sample = database.upsert_voice_library_sample(
                {
                    "sample_key": "test:sample:1",
                    "person_id": profile["id"],
                    "identity_label": "我",
                    "sample_type": "confirmed_conversation",
                    "split": "holdout",
                    "source_path": values["source_path"],
                    "recording_id": 1,
                    "segment_id": first["id"],
                    "session_key": "recording:1",
                    "duration_ms": 800,
                    "speech_ms": 800,
                    "human_confirmed": True,
                }
            )
            self.assertEqual(sample["sample_key"], "test:sample:1")
            self.assertEqual(len(database.list_voice_library_samples(person_id=profile["id"])), 1)
            self.assertTrue(database.delete_person_profile(profile["id"]))
            self.assertIsNone(database.get_self_profile())
        finally:
            for suffix in ("", "-shm", "-wal"):
                candidate = Path(f"{database_path}{suffix}")
                if candidate.exists():
                    candidate.unlink()


if __name__ == "__main__":
    unittest.main()
