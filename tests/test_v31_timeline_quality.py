from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from typer.testing import CliRunner

from allday_asr.cli import app
from allday_asr.v3.application.timeline_quality import run_timeline_quality_audit
from allday_asr.v3.domain.timeline_quality import (
    TIMELINE_AUDIT_FORMAT,
    evaluate_timeline_quality,
    parse_timeline_audit_document,
)


class V31TimelineQualityTests(unittest.TestCase):
    def test_clean_reviewed_seams_are_accepted(self) -> None:
        document = _accepted_document()
        _, completeness, seams, references, predictions = (
            parse_timeline_audit_document(document)
        )

        decision = evaluate_timeline_quality(
            seams, references, predictions, completeness
        )

        self.assertTrue(decision.accepted, decision.blockers)
        self.assertEqual(decision.blockers, ())
        self.assertEqual(decision.metrics["seam_count"], 10)
        self.assertEqual(decision.metrics["reference_utterances"], 40)
        self.assertEqual(decision.metrics["overlap"]["reference_utterances"], 20)
        self.assertEqual(decision.metrics["boundary"]["p95_absolute_error_ms"], 0)
        self.assertEqual(decision.metrics["asr"]["character_error_rate"], 0)
        self.assertEqual(decision.metrics["speaker"]["continuity_checks"], 20)
        self.assertEqual(decision.metrics["speaker"]["continuity_error_rate"], 0)

    def test_incomplete_or_drifting_seams_fail_closed(self) -> None:
        document = _accepted_document()
        document["truth_completeness"]["alignment"] = "sparse"
        for prediction in document["predicted_utterances"]:
            prediction["start_ms"] += 1_000
            prediction["end_ms"] += 1_000
            prediction["text"] = "错"
            if prediction["utterance_id"].endswith("-right"):
                prediction["speaker_id"] = "fragmented-right-track"
        document["predicted_utterances"].pop(0)
        _, completeness, seams, references, predictions = (
            parse_timeline_audit_document(document)
        )

        decision = evaluate_timeline_quality(
            seams, references, predictions, completeness
        )

        self.assertFalse(decision.accepted)
        self.assertIn("alignment_truth_not_exhaustive", decision.blockers)
        self.assertIn("boundary_p95_exceeds_limit", decision.blockers)
        self.assertIn("seam_cer_exceeds_limit", decision.blockers)
        self.assertIn(
            "speaker_continuity_error_rate_exceeds_limit", decision.blockers
        )
        self.assertIn("overlap_error_rate_exceeds_limit", decision.blockers)

    def test_hashed_receipt_is_deterministic_and_cli_is_machine_readable(self) -> None:
        directory = Path(__file__).parent / f"v31c-{uuid4().hex}"
        directory.mkdir()
        source = directory / "audit.json"
        first = directory / "first.json"
        second = directory / "second.json"
        try:
            source.write_text(
                json.dumps(_accepted_document(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            first_summary = run_timeline_quality_audit(
                source, receipt_path=first
            )
            second_summary = run_timeline_quality_audit(
                source, receipt_path=second
            )
            self.assertTrue(first_summary.accepted)
            self.assertEqual(
                first_summary.receipt_sha256, second_summary.receipt_sha256
            )
            self.assertEqual(json.loads(first.read_text(encoding="utf-8"))["decision"], "accepted")

            result = CliRunner().invoke(
                app,
                ["seam-audit", str(source), "--output", str(directory / "cli.json")],
                env={"ALLDAY_V3_ENABLED": "1"},
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue(json.loads(result.output)["accepted"])
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_seams_must_be_independent_and_predictions_trace_to_joined_chunks(self) -> None:
        document = _accepted_document()
        document["seams"][1]["offset_ms"] = document["seams"][0]["offset_ms"] + 1_000
        document["predicted_utterances"][0]["source_chunk_ids"] = ["unrelated"]
        _, completeness, seams, references, predictions = (
            parse_timeline_audit_document(document)
        )

        decision = evaluate_timeline_quality(
            seams, references, predictions, completeness
        )

        self.assertFalse(decision.accepted)
        self.assertIn("seam_windows_overlap", decision.blockers)
        self.assertIn("prediction_chunk_provenance_mismatch", decision.blockers)


def _accepted_document() -> dict:
    seams: list[dict] = []
    references: list[dict] = []
    predictions: list[dict] = []
    for index in range(10):
        offset = (index + 1) * 100_000
        left_chunk = f"chunk-{index:02d}"
        right_chunk = f"chunk-{index + 1:02d}"
        seams.append(
            {
                "seam_id": f"seam-{index:02d}",
                "offset_ms": offset,
                "left_chunk_id": left_chunk,
                "right_chunk_id": right_chunk,
                "reviewed": True,
            }
        )
        values = (
            ("left", offset - 3_000, offset - 1_500, "甲说左侧", "person-a", True, left_chunk, "track-a"),
            ("overlap", offset - 2_800, offset - 1_700, "乙在重叠", "person-b", True, left_chunk, "track-b"),
            ("right", offset + 500, offset + 2_000, "甲说右侧", "person-a", False, right_chunk, "track-a"),
            ("later", offset + 2_300, offset + 3_600, "乙说右侧", "person-b", False, right_chunk, "track-b"),
        )
        for label, start, end, text, speaker, overlap, chunk, track in values:
            utterance_id = f"{index:02d}-{label}"
            references.append(
                {
                    "utterance_id": f"ref-{utterance_id}",
                    "start_ms": start,
                    "end_ms": end,
                    "text": text,
                    "speaker_id": speaker,
                    "has_overlap": overlap,
                }
            )
            predictions.append(
                {
                    "utterance_id": f"pred-{utterance_id}",
                    "start_ms": start,
                    "end_ms": end,
                    "text": text,
                    "speaker_id": track,
                    "source_chunk_ids": [chunk],
                }
            )
    return {
        "format": TIMELINE_AUDIT_FORMAT,
        "session_id": "01990d5a-7c00-7000-8000-000000000001",
        "truth_completeness": {
            "alignment": "exhaustive",
            "transcript": "exhaustive",
            "speaker": "exhaustive",
            "overlap": "exhaustive",
        },
        "seams": seams,
        "reference_utterances": references,
        "predicted_utterances": predictions,
        "metadata": {"reviewer": "independent-human", "blind": True},
    }


if __name__ == "__main__":
    unittest.main()
