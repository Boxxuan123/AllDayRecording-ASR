from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from allday_asr.application.workflows import quality_stages as stages
from allday_asr.application.workflows.quality_models import (
    ReviewReason,
    WorkflowContext,
)
from allday_asr.services.quality_asr import QualityAsrSettings


class QualityWorkflowStageTests(unittest.TestCase):
    def test_review_reason_serializes_structured_details(self) -> None:
        reason = ReviewReason(
            code="review-code",
            stage="test-stage",
            message="需要人工检查",
            details={"count": 3},
        )

        self.assertEqual(
            reason.to_dict(),
            {
                "code": "review-code",
                "stage": "test-stage",
                "message": "需要人工检查",
                "count": 3,
            },
        )

    def test_asr_stage_resumes_matching_failed_run(self) -> None:
        database = Mock()
        settings = QualityAsrSettings(model_signature="stage-test-asr")
        database.list_session_processing_runs.return_value = [
            {
                "id": 41,
                "run_kind": "quality_asr_v2c",
                "config_sha256": settings.sha256(),
                "input_fingerprint": "input-fingerprint",
                "status": "failed",
            }
        ]
        context = WorkflowContext(
            workflow_run_id=1,
            session_id=2,
            recording_id=None,
            input_fingerprint="input-fingerprint",
            admission_mode="shadow",
        )
        progress: list[tuple[str, str]] = []

        with patch.object(
            stages,
            "run_quality_asr",
            return_value=SimpleNamespace(run_id=42),
        ) as run_mock:
            result = stages.resolve_or_run_asr(
                database,
                context,
                settings=settings,
                primary_factory=lambda: object(),
                secondary_factory=lambda: object(),
                report=lambda stage, detail: progress.append((stage, detail)),
            )

        self.assertEqual(result.run_id, 42)
        self.assertFalse(result.reused)
        self.assertEqual(run_mock.call_args.kwargs["resume_run_id"], 41)
        self.assertEqual(progress[0][0], "asr_running")

    def test_identity_audit_failure_becomes_review_reason(self) -> None:
        database = Mock()
        database.list_session_processing_runs.return_value = []
        context = WorkflowContext(
            workflow_run_id=1,
            session_id=2,
            recording_id=None,
            input_fingerprint="input-fingerprint",
            admission_mode="shadow",
        )

        with (
            patch.object(
                stages,
                "_latest_frozen_speaker_truth_set",
                return_value={"id": 9},
            ),
            patch.object(
                stages,
                "run_identity_contamination_audit",
                side_effect=RuntimeError("audit failed"),
            ),
        ):
            result = stages.run_optional_identity_audit(
                database,
                context,
                diarization_run_id=3,
                report=lambda _stage, _detail: None,
            )

        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.run_id)
        self.assertEqual(result.review_reasons[0].code, "v2d2_failed")
        self.assertIn("audit failed", str(result.summary["error"]))


if __name__ == "__main__":
    unittest.main()
