from __future__ import annotations

import hashlib
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from allday_asr.services.speaker_timeline import (
    speaker_timeline_overview,
    speaker_timeline_window,
)
from allday_asr.storage.database import Database
from allday_asr.web import WebApplication


class SpeakerTimelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parent
        self.token = uuid4().hex
        self.database_path = self.root / f"speaker-timeline-{self.token}.sqlite3"
        self.source_path = self.root / f"speaker-timeline-{self.token}.m4a"
        self.output_path = self.root / f"speaker-timeline-output-{self.token}"
        self.source_bytes = b"permanent-original-watch-audio"
        self.source_path.write_bytes(self.source_bytes)
        self.database = Database.open(self.database_path)
        recording = self.database.create_recording(
            {
                "source_path": str(self.source_path.resolve()),
                "sha256": hashlib.sha256(self.source_bytes).hexdigest(),
                "device": "watch",
                "recorded_at": "2026-08-28T00:00:00+00:00",
                "timezone": "Asia/Singapore",
                "duration_ms": 300_000,
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
        self.asr_run_id, self.token_rows = self._create_asr_run()
        self.diarization_run_id = self._create_diarization_run()

    def tearDown(self) -> None:
        if self.output_path.exists():
            shutil.rmtree(self.output_path)
        self.source_path.unlink(missing_ok=True)
        for suffix in ("", "-shm", "-wal"):
            Path(f"{self.database_path}{suffix}").unlink(missing_ok=True)
        for backup in self.root.glob(
            f"speaker-timeline-{self.token}.schema-*.sqlite3"
        ):
            backup.unlink(missing_ok=True)

    def test_ranks_multi_speaker_meal_and_groups_unassigned_tokens(self) -> None:
        overview = speaker_timeline_overview(self.database, self.recording_id)

        self.assertTrue(overview["available"])
        self.assertEqual(overview["run"]["id"], self.diarization_run_id)
        self.assertEqual(len(overview["speakers"]), 3)
        self.assertEqual(overview["speech_ms"], 180_000)
        top = overview["queues"]["conversation"]["items"][0]
        self.assertGreaterEqual(top["start_ms"], 90_000)
        self.assertLess(top["start_ms"], 180_000)
        self.assertGreaterEqual(top["speaker_count"], 2)
        self.assertGreater(top["speaker_switches"], 10)
        self.assertEqual(overview["queues"]["overlap"]["count"], 1)
        self.assertEqual(overview["queues"]["unassigned"]["token_count"], 2)
        self.assertEqual(overview["queues"]["unassigned"]["count"], 1)

        window = speaker_timeline_window(
            self.database,
            self.recording_id,
            run_id=self.diarization_run_id,
            start_ms=120_000,
            end_ms=210_000,
        )
        self.assertGreater(len(window["turns"]), 10)
        self.assertEqual(len(window["overlaps"]), 1)
        self.assertIn("/api/speaker-timeline/audio", window["audio_url"])

    def test_listening_clip_is_derived_without_changing_original(self) -> None:
        application = WebApplication(
            self.database_path, self.root / "unused.toml", token="test-token"
        )
        with (
            patch(
                "allday_asr.interfaces.web.use_cases.media.recording_output_dir",
                return_value=self.output_path,
            ),
            patch(
                "allday_asr.interfaces.web.use_cases.media.materialize_logical_window"
            ) as materialize,
        ):
            destination = application.speaker_timeline_audio_clip(
                self.recording_id, start_ms=120_000, end_ms=210_000
            )

        self.assertEqual(destination.parent.name, "web-speaker-timeline-audio-v1")
        self.assertIn("range-120000-210000", destination.name)
        self.assertEqual(materialize.call_count, 1)
        window = materialize.call_args.args[0]
        self.assertEqual(window.analysis_start_ms, 120_000)
        self.assertEqual(window.analysis_end_ms, 210_000)
        self.assertEqual(
            materialize.call_args.kwargs["audio_filter"],
            "loudnorm=I=-18:LRA=7:TP=-2",
        )
        self.assertEqual(self.source_path.read_bytes(), self.source_bytes)

    def _create_asr_run(self):
        run_id = self.database.start_processing_run(
            self.recording_id,
            run_kind="quality_asr_v2c",
            config={"test": True},
            config_sha256="speaker-timeline-asr",
            model_manifest={"primary": {"model_id": "fake/qwen"}},
            pipeline_version="v2-c",
        )
        token_values = []
        index = 0
        for start_ms, end_ms, text in (
            (10_000, 11_000, "电视"),
            (40_000, 41_000, "节目"),
            *[
                (120_000 + offset, 121_000 + offset, f"饭{turn_index}")
                for turn_index, offset in enumerate(range(0, 90_000, 5_000))
            ],
            (250_000, 251_000, "远处"),
            (252_000, 253_000, "声音"),
        ):
            token_values.append(
                {
                    "token_index": index,
                    "text": text,
                    "session_start_ms": start_ms,
                    "session_end_ms": end_ms,
                    "analysis_start_ms": start_ms,
                    "analysis_end_ms": end_ms,
                    "kept_in_core": True,
                    "alignment_model_id": "fake/aligner",
                    "source_refs": [self._source_ref(start_ms, end_ms)],
                }
            )
            index += 1
        self.database.create_asr_hypothesis(
            {
                "hypothesis_key": f"speaker-timeline:{self.token}:primary",
                "run_id": run_id,
                "session_id": int(self.session["id"]),
                "window_index": 0,
                "hypothesis_role": "primary",
                "core_start_ms": 0,
                "core_end_ms": 300_000,
                "analysis_start_ms": 0,
                "analysis_end_ms": 300_000,
                "model_id": "fake/qwen",
                "backend": "fake",
                "text": "".join(value["text"] for value in token_values),
            },
            token_values,
        )
        self.database.finish_processing_run(
            run_id, status="completed", summary={"tokens": len(token_values)}
        )
        return run_id, self.database.list_committed_asr_tokens(run_id)

    def _create_diarization_run(self) -> int:
        run_id = self.database.start_processing_run(
            self.recording_id,
            run_kind="quality_diarization_v2d",
            config={"asr_run_id": self.asr_run_id},
            config_sha256="speaker-timeline-diarization",
            model_manifest={
                "model_id": "pyannote/speaker-diarization-community-1",
                "model_revision": "test-revision",
                "backend": "fake",
                "privacy": "local-inference-no-audio-upload",
            },
            pipeline_version="v2-d",
            parent_run_id=self.asr_run_id,
        )
        exclusive = [(0, 90_000, "SPEAKER_TV")]
        exclusive.extend(
            (
                120_000 + offset,
                125_000 + offset,
                "SPEAKER_A" if turn_index % 2 == 0 else "SPEAKER_B",
            )
            for turn_index, offset in enumerate(range(0, 90_000, 5_000))
        )
        regular = [*exclusive, (132_000, 135_000, "SPEAKER_B")]
        values = []
        for kind, rows in (("regular", regular), ("exclusive", exclusive)):
            for turn_index, (start_ms, end_ms, label) in enumerate(rows):
                values.append(
                    {
                        "turn_key": f"{self.token}:{kind}:{turn_index}",
                        "turn_index": turn_index,
                        "turn_kind": kind,
                        "speaker_label": label,
                        "session_start_ms": start_ms,
                        "session_end_ms": end_ms,
                        "source_refs": [self._source_ref(start_ms, end_ms)],
                    }
                )
        self.database.create_diarization_turns(
            run_id, int(self.session["id"]), values
        )
        attributions = []
        for token in self.token_rows:
            token_id = int(token["id"])
            start_ms = int(token["session_start_ms"])
            if start_ms >= 250_000:
                label = None
                kind = "none"
            elif start_ms < 90_000:
                label = "SPEAKER_TV"
                kind = "primary"
            else:
                turn_index = (start_ms - 120_000) // 5_000
                label = "SPEAKER_A" if turn_index % 2 == 0 else "SPEAKER_B"
                kind = "primary"
            attributions.append(
                {
                    "attribution_key": f"{self.token}:token:{token_id}",
                    "token_id": token_id,
                    "speaker_label": label,
                    "attribution_kind": kind,
                    "overlap_ms": 1_000 if label else 0,
                    "overlap_ratio": 1.0 if label else 0.0,
                    "rank": 0,
                    "confidence": 1.0 if label else 0.0,
                }
            )
        self.database.create_token_speaker_attributions(
            run_id, self.asr_run_id, attributions
        )
        self.database.finish_processing_run(
            run_id,
            status="completed",
            summary={
                "asr_run_id": self.asr_run_id,
                "speakers": 3,
                "overlap_regions": 1,
                "unassigned_tokens": 2,
            },
        )
        return run_id

    def _source_ref(self, start_ms: int, end_ms: int) -> dict:
        return {
            "source_object_id": int(self.source["source_object_id"]),
            "source_sha256": str(self.source["sha256"]),
            "source_start_ms": start_ms,
            "source_end_ms": end_ms,
        }


if __name__ == "__main__":
    unittest.main()
