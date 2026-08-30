from __future__ import annotations

import hashlib
import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from allday_asr.services.benchmark import (
    CONTINUOUS_TRUTH_FORMAT,
    import_continuous_truth,
)
from allday_asr.services.manual_identity import (
    manual_identity_overview,
    retract_manual_identity_annotation,
    save_manual_identity_annotation,
)
from allday_asr.services.quality_diarization_v2d1 import (
    V2D1Settings,
    create_source_micro_truth,
    run_quality_diarization_v2d1,
)
from allday_asr.services.quality_diarization_v2d1_review import (
    complete_possible_speech_review,
    effective_workflow_summary,
    label_possible_speech_identity,
    review_possible_speech_candidate,
)
from allday_asr.services.quality_diarization_v2d1_truth import (
    create_v2d1_review_truth,
    evaluate_v2d1_review,
)
from allday_asr.services.quality_diarization_v2d2 import (
    run_identity_contamination_audit,
)
from allday_asr.services.speaker_timeline import (
    speaker_timeline_overview,
    speaker_timeline_window,
)
from allday_asr.storage.database import Database


class QualityDiarizationV2D1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parent
        self.token = uuid4().hex
        self.database_path = self.root / f"v2d1-{self.token}.sqlite3"
        self.source_path = self.root / f"v2d1-{self.token}.m4a"
        self.output_dir = self.root / f"v2d1-output-{self.token}"
        self.evaluation_dir = self.root / f"v2d1-evaluation-{self.token}"
        self.source_bytes = b"immutable-v2d1-watch-audio"
        self.source_path.write_bytes(self.source_bytes)
        self.database = Database.open(self.database_path)
        recording = self.database.create_recording(
            {
                "source_path": str(self.source_path.resolve()),
                "sha256": hashlib.sha256(self.source_bytes).hexdigest(),
                "device": "watch",
                "recorded_at": "2026-08-29T00:00:00+00:00",
                "timezone": "Asia/Singapore",
                "duration_ms": 10_000,
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
        self.diarization_run_id = self._create_diarization_run()

    def tearDown(self) -> None:
        for directory in (self.output_dir, self.evaluation_dir):
            if directory.exists():
                shutil.rmtree(directory)
        self.source_path.unlink(missing_ok=True)
        for suffix in ("", "-shm", "-wal"):
            Path(f"{self.database_path}{suffix}").unlink(missing_ok=True)
        for backup in self.root.glob(f"v2d1-{self.token}.schema-*.sqlite3"):
            backup.unlink(missing_ok=True)

    def test_recall_rescue_is_separate_from_speakers_and_source_truth(self) -> None:
        with patch(
            "allday_asr.services.quality_diarization_v2d1.EVALUATION_DIR",
            self.evaluation_dir,
        ):
            truth = create_source_micro_truth(
                self.database,
                self.recording_id,
                name=f"v2d1-media-{self.token}",
                start_ms=0,
                end_ms=4_000,
                speech_source="media_playback",
                notes=("two TV speakers stay separate",),
            )

        with (
            patch(
                "allday_asr.services.quality_diarization_v2d1.OUTPUT_DIR",
                self.output_dir,
            ),
            patch("allday_asr.services.benchmark.OUTPUT_DIR", self.output_dir),
        ):
            summary = run_quality_diarization_v2d1(
                self.database,
                None,
                session_id=int(self.session["id"]),
                diarization_run_id=self.diarization_run_id,
                settings=V2D1Settings(bridge_gap_ms=4_000),
                evaluation_truth_set_ids=(truth.truth_set_id,),
            )

        self.assertGreaterEqual(summary.possible_ms, 2_000)
        self.assertEqual(summary.detected_ms, 2_000)
        metrics = summary.evaluations[str(truth.truth_set_id)]
        self.assertEqual(metrics["detected"]["vad"]["recall"], 0.5)
        self.assertEqual(metrics["recall_rescue"]["vad"]["recall"], 1.0)
        rescue = self.database.list_benchmark_predictions(
            summary.rescue_prediction_set_id, prediction_kind="speech"
        )
        possible = [
            row
            for row in rescue
            if '"tier": "possible"' in str(row["metadata_json"])
        ]
        self.assertEqual(
            [(row["session_start_ms"], row["session_end_ms"]) for row in possible],
            [(1_000, 3_000)],
        )
        parent_labels = {
            str(row["speaker_label"])
            for row in self.database.list_diarization_turns(
                self.diarization_run_id, turn_kind="regular"
            )
        }
        self.assertEqual(parent_labels, {"SPEAKER_TV_MALE", "SPEAKER_TV_FEMALE"})
        truth_row = self.database.list_truth_annotations(truth.truth_set_id)[0]
        self.assertIn("media_playback", str(truth_row["metadata_json"]))

        identity_truth = self._create_identity_truth()
        with patch(
            "allday_asr.services.quality_diarization_v2d2.OUTPUT_DIR",
            self.output_dir,
        ):
            identity_audit = run_identity_contamination_audit(
                self.database,
                None,
                session_id=int(self.session["id"]),
                diarization_run_id=self.diarization_run_id,
                truth_set_id=identity_truth,
            )
        self.assertEqual(
            identity_audit.contaminated_speakers,
            ["SPEAKER_TV_MALE"],
        )
        male_audit = next(
            item
            for item in identity_audit.model_speakers
            if item["speaker"] == "SPEAKER_TV_MALE"
        )
        self.assertEqual(
            {item["identity"] for item in male_audit["identities"]},
            {"mother", "tv"},
        )

        overview = speaker_timeline_overview(self.database, self.recording_id)
        self.assertTrue(overview["v2d1"]["available"])
        self.assertEqual(overview["v2d1"]["run_id"], summary.run_id)
        self.assertTrue(overview["v2d2"]["available"])
        self.assertEqual(overview["v2d2"]["run_id"], identity_audit.run_id)
        self.assertEqual(overview["queues"]["possible"]["count"], 1)
        possible_candidate = overview["queues"]["possible"]["items"][0]
        with self.assertRaisesRegex(ValueError, "还有 1 个"):
            complete_possible_speech_review(self.database, summary.run_id)
        reviewed = review_possible_speech_candidate(
            self.database,
            summary.run_id,
            candidate_id=possible_candidate["id"],
            status="confirmed_speech",
        )
        self.assertEqual(reviewed["review"]["reviewed_count"], 1)
        labeled = label_possible_speech_identity(
            self.database,
            summary.run_id,
            candidate_id=possible_candidate["id"],
            identity_label="tv",
        )
        self.assertEqual(labeled["review"]["identity_labeled_count"], 1)
        reviewed_overview = speaker_timeline_overview(
            self.database, self.recording_id
        )
        self.assertEqual(
            reviewed_overview["queues"]["possible"]["items"][0][
                "review_status"
            ],
            "confirmed_speech",
        )
        completion = complete_possible_speech_review(
            self.database, summary.run_id
        )
        self.assertTrue(completion["completed"])
        with patch(
            "allday_asr.services.benchmark.OUTPUT_DIR", self.output_dir
        ):
            evaluation = evaluate_v2d1_review(
                self.database,
                summary.run_id,
                output_dir=self.evaluation_dir,
            )
        self.assertEqual(
            evaluation["benchmarks"]["detected"]["vad"]["recall"], 0.0
        )
        self.assertEqual(
            evaluation["benchmarks"]["recall_rescue"]["vad"]["recall"], 1.0
        )
        saved_identity = save_manual_identity_annotation(
            self.database,
            session_id=int(self.session["id"]),
            diarization_run_id=self.diarization_run_id,
            start_ms=3_000,
            end_ms=4_000,
            identity_label="father",
        )
        self.assertEqual(saved_identity["overview"]["identity_counts"], {"father": 1})
        identity_review_truth = create_v2d1_review_truth(
            self.database,
            summary.run_id,
            include_identities=True,
            output_dir=self.evaluation_dir,
        )
        identity_rows = self.database.list_truth_annotations(
            identity_review_truth["truth_set_id"], annotation_kind="speaker"
        )
        self.assertEqual(
            [str(row["label"]) for row in identity_rows], ["tv", "father"]
        )
        manual_row = next(
            row
            for row in identity_rows
            if str(row["label"]) == "father"
        )
        self.assertIn(
            "manual_identity_annotation_id", str(manual_row["metadata_json"])
        )
        effective = effective_workflow_summary(
            self.database,
            {
                "workflow_state": "semantic_ready_needs_review",
                "base_state": "semantic_ready",
                "enhancements": {"v2d1": {"run_id": summary.run_id}},
                "review": {
                    "required": True,
                    "reasons": [
                        {"code": "possible_speech_high", "stage": "v2d1"}
                    ],
                },
            },
        )
        self.assertEqual(effective["workflow_state"], "semantic_ready")
        self.assertFalse(effective["review"]["required"])
        window = speaker_timeline_window(
            self.database,
            self.recording_id,
            run_id=self.diarization_run_id,
            start_ms=0,
            end_ms=4_000,
        )
        self.assertEqual(
            {turn["speaker"] for turn in window["turns"]},
            {"SPEAKER_TV_MALE", "SPEAKER_TV_FEMALE"},
        )
        self.assertEqual(
            {item["tier"] for item in window["speech_evidence"]},
            {"detected", "possible"},
        )
        self.assertEqual(
            {item["source"] for item in window["source_regions"]},
            {"media_playback"},
        )
        self.assertEqual(
            {item["identity"] for item in window["identity_regions"]},
            {"mother", "tv", "father"},
        )
        male_turn = next(
            item
            for item in window["turns"]
            if item["speaker"] == "SPEAKER_TV_MALE"
        )
        self.assertEqual(
            {
                item["identity"]
                for item in male_turn["identity_evidence"]["evidence"]
            },
            {"mother", "tv"},
        )
        annotation_id = int(saved_identity["annotation"]["id"])
        retracted = retract_manual_identity_annotation(
            self.database,
            annotation_id,
            session_id=int(self.session["id"]),
            diarization_run_id=self.diarization_run_id,
        )
        self.assertEqual(retracted["overview"]["count"], 0)
        self.assertEqual(
            manual_identity_overview(
                self.database,
                int(self.session["id"]),
                diarization_run_id=self.diarization_run_id,
            )["items"],
            [],
        )
        self.assertEqual(self.source_path.read_bytes(), self.source_bytes)

    def _create_identity_truth(self) -> int:
        path = self.evaluation_dir / f"v2d2-identities-{self.token}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "type": "metadata",
                "format": CONTINUOUS_TRUTH_FORMAT,
                "name": f"v2d2-identities-{self.token}",
                "session_id": int(self.session["id"]),
                "scope_start_ms": 0,
                "scope_end_ms": 4_000,
                "input_fingerprint": self.database.session_input_fingerprint(
                    int(self.session["id"])
                ),
                "completeness": {
                    "vad": "none",
                    "transcript": "none",
                    "speaker": "sparse",
                    "identity": "sparse",
                    "overlap": "none",
                    "alignment": "none",
                    "entities": "none",
                },
                "provenance": {"kind": "synthetic-v2d2-test"},
            },
            *[
                {
                    "type": "annotation",
                    "key": f"identity:{index}",
                    "kind": "speaker",
                    "session_start_ms": start_ms,
                    "session_end_ms": end_ms,
                    "label": label,
                    "text": None,
                    "metadata": {"reviewed": True},
                }
                for index, (start_ms, end_ms, label) in enumerate(
                    (
                        (0, 400, "mother"),
                        (400, 1_000, "tv"),
                        (3_000, 4_000, "father"),
                    )
                )
            ],
        ]
        path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )
        return import_continuous_truth(self.database, path).truth_set_id

    def _create_asr_run(self) -> int:
        run_id = self.database.start_processing_run(
            self.recording_id,
            run_kind="quality_asr_v2c",
            config={"test": True},
            config_sha256="v2d1-asr",
            model_manifest={"primary": {"model_id": "fake/qwen"}},
            pipeline_version="v2-c",
        )
        self.database.create_asr_hypothesis(
            {
                "hypothesis_key": f"v2d1:{self.token}:primary",
                "run_id": run_id,
                "session_id": int(self.session["id"]),
                "window_index": 0,
                "hypothesis_role": "primary",
                "core_start_ms": 0,
                "core_end_ms": 10_000,
                "analysis_start_ms": 0,
                "analysis_end_ms": 10_000,
                "model_id": "fake/qwen",
                "backend": "fake",
                "text": "",
                "raw_response": {
                    "speech_ranges_ms": [[0, 1_000], [3_000, 4_000]],
                    "segments": [
                        {
                            "candidate_index": 0,
                            "core_start_ms": 1_200,
                            "core_end_ms": 1_700,
                            "duration_ms": 500,
                            "snr_db": 4.0,
                            "silero_overlap_ms": 0,
                            "accepted": False,
                            "acceptance_reasons": [],
                            "text": "电视对白",
                            "aligned_token_count": 4,
                        }
                    ],
                },
            },
            [],
        )
        self.database.finish_processing_run(
            run_id, status="completed", summary={"tokens": 0}
        )
        return run_id

    def _create_diarization_run(self) -> int:
        run_id = self.database.start_processing_run(
            self.recording_id,
            run_kind="quality_diarization_v2d",
            config={"asr_run_id": self.asr_run_id},
            config_sha256="v2d1-diarization",
            model_manifest={"model_id": "fake/community-1"},
            pipeline_version="v2-d",
            parent_run_id=self.asr_run_id,
        )
        values = []
        for kind in ("regular", "exclusive"):
            for index, (start_ms, end_ms, label) in enumerate(
                (
                    (0, 1_000, "SPEAKER_TV_MALE"),
                    (3_000, 4_000, "SPEAKER_TV_FEMALE"),
                )
            ):
                values.append(
                    {
                        "turn_key": f"{self.token}:{kind}:{index}",
                        "turn_index": index,
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
        self.database.finish_processing_run(
            run_id,
            status="completed",
            summary={"asr_run_id": self.asr_run_id, "speakers": 2},
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
