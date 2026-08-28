from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from allday_asr.services.evaluation import (
    EVALUATION_FORMAT,
    evaluate_truth,
    levenshtein_operations,
    normalize_text,
    parse_offset,
)
from allday_asr.storage.database import Database


class EvaluationTests(unittest.TestCase):
    def test_text_normalization_offsets_and_edit_operations(self) -> None:
        self.assertEqual(parse_offset("01:02:03.5"), 3_723_500)
        self.assertEqual(normalize_text(" 明天，10 点！"), "明天10点")
        self.assertEqual(
            levenshtein_operations("你好", "你们好"),
            {"distance": 1, "substitutions": 0, "deletions": 0, "insertions": 1},
        )

    def test_evaluation_report_uses_current_database_predictions(self) -> None:
        suffix = uuid4().hex
        database_path = Path(__file__).parent / f"evaluation-{suffix}.sqlite3"
        truth_path = Path(__file__).parent / f"truth-{suffix}.jsonl"
        output_dir = Path(__file__).parent / f"evaluation-output-{suffix}"
        try:
            output_dir.mkdir()
            database = Database(database_path)
            recording = database.create_recording(
                {
                    "source_path": str(Path(__file__).parent / "audio.m4a"),
                    "sha256": f"evaluation-{suffix}",
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
            database.replace_vad_segments(
                recording_id,
                [(0, 2_000), (3_000, 4_000)],
                str(Path(__file__).parent / "audio.m4a"),
            )
            segments = database.all_segments(recording_id)
            for segment, text in zip(segments, ["明天十点饭店见", "好的"], strict=True):
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
            rows = [
                {
                    "type": "metadata",
                    "format": EVALUATION_FORMAT,
                    "name": "test",
                    "recording_id": recording_id,
                },
                {
                    "type": "segment",
                    "include": True,
                    "segment_id": int(segments[0]["id"]),
                    "reference_text": "明天十点饭店见",
                    "reference_speaker": "mother",
                    "reference_identity": "not_self",
                    "key_facts": ["明天十点", "饭店"],
                },
                {
                    "type": "segment",
                    "include": True,
                    "segment_id": int(segments[1]["id"]),
                    "reference_text": "好的",
                    "reference_speaker": "self",
                    "reference_identity": "self",
                    "key_facts": ["好的"],
                },
            ]
            truth_path.write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
                encoding="utf-8",
            )
            with patch(
                "allday_asr.services.evaluation.recording_output_dir",
                return_value=output_dir,
            ):
                summary = evaluate_truth(database, truth_path)

            self.assertEqual(summary.metrics["text"]["cer"], 0)
            self.assertEqual(summary.metrics["self_identity"]["accuracy"], 1)
            self.assertEqual(summary.metrics["key_facts"]["recall"], 1)
            self.assertTrue(summary.report_markdown_path.is_file())
            self.assertEqual(len(database.list_evaluation_runs(recording_id)), 1)
        finally:
            if output_dir.exists():
                shutil.rmtree(output_dir)
            if truth_path.exists():
                truth_path.unlink()
            for database_suffix in ("", "-shm", "-wal"):
                candidate = Path(f"{database_path}{database_suffix}")
                if candidate.exists():
                    candidate.unlink()


if __name__ == "__main__":
    unittest.main()
