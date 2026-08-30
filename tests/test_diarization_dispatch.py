from __future__ import annotations

import ast
import inspect
import unittest
from unittest.mock import MagicMock, patch

from allday_asr.application.diarization import (
    base,
    identity_audit,
    identity_candidates,
    manual_identity,
    recall_pipeline,
    recall_review,
    timeline,
    truth,
)
from allday_asr.application.diarization.contracts import (
    DEFAULT_DIARIZATION_VERSION,
    DiarizationVersion,
    require_diarization_version,
)
from allday_asr.application.diarization.identity_audit import (
    run_identity_contamination_audit,
)
from allday_asr.application.diarization.identity_candidates import (
    run_identity_candidate_mining,
)
from allday_asr.application.diarization.pipeline import run
from allday_asr.application.diarization.speech_recall import (
    run_quality_diarization_v2d1,
)
from allday_asr.application.diarization.versions import runner_for
from allday_asr.services import (
    manual_identity as manual_identity_compat,
    quality_diarization as base_compat,
    quality_diarization_v2d1 as recall_compat,
    quality_diarization_v2d2 as identity_audit_compat,
    quality_diarization_v2d3 as identity_candidates_compat,
    speaker_timeline as timeline_compat,
)


class DiarizationVersionDispatchTests(unittest.TestCase):
    def test_versions_map_to_explicit_responsibility_runners(self) -> None:
        self.assertEqual(DEFAULT_DIARIZATION_VERSION, DiarizationVersion.V2_D)
        self.assertEqual(
            runner_for("v2-d").__name__,
            "run_quality_diarization",
        )
        self.assertIs(
            runner_for("v2-d.1"),
            run_quality_diarization_v2d1,
        )
        self.assertIs(
            runner_for("v2-d.2"),
            run_identity_contamination_audit,
        )
        self.assertIs(
            runner_for("v2-d.3"),
            run_identity_candidate_mining,
        )

    def test_unknown_version_fails_without_silent_fallback(self) -> None:
        with self.assertRaisesRegex(ValueError, "不支持的 diarization"):
            require_diarization_version("v2-d.future")
        with self.assertRaisesRegex(ValueError, "不支持的 diarization"):
            runner_for("v2-d.future")

    def test_stable_pipeline_has_one_base_default(self) -> None:
        database = MagicMock()
        dispatched = MagicMock(return_value={"run_id": 4})
        with patch(
            "allday_asr.application.diarization.pipeline.runner_for",
            return_value=dispatched,
        ) as select:
            result = run(database, 2, session_id=3)
        select.assert_called_once_with(DiarizationVersion.V2_D)
        dispatched.assert_called_once_with(database, 2, session_id=3)
        self.assertEqual(result, {"run_id": 4})

    def test_versioned_service_paths_are_thin_compatibility_exports(self) -> None:
        self.assertIs(base_compat.run_quality_diarization, base.run_quality_diarization)
        self.assertIs(
            recall_compat.run_quality_diarization_v2d1,
            recall_pipeline.run_quality_diarization_v2d1,
        )
        self.assertIs(
            identity_audit_compat.run_identity_contamination_audit,
            identity_audit.run_identity_contamination_audit,
        )
        self.assertIs(
            identity_candidates_compat.run_identity_candidate_mining,
            identity_candidates.run_identity_candidate_mining,
        )
        self.assertIs(
            manual_identity_compat.save_manual_identity_annotation,
            manual_identity.save_manual_identity_annotation,
        )
        self.assertIs(
            timeline_compat.speaker_timeline_overview,
            timeline.speaker_timeline_overview,
        )

    def test_application_modules_do_not_depend_on_versioned_service_files(self) -> None:
        forbidden = {
            "allday_asr.services.manual_identity",
            "allday_asr.services.quality_diarization",
            "allday_asr.services.quality_diarization_v2d1",
            "allday_asr.services.quality_diarization_v2d1_review",
            "allday_asr.services.quality_diarization_v2d1_truth",
            "allday_asr.services.quality_diarization_v2d2",
            "allday_asr.services.quality_diarization_v2d3",
            "allday_asr.services.speaker_timeline",
        }
        modules = (
            base,
            recall_pipeline,
            recall_review,
            manual_identity,
            truth,
            identity_audit,
            identity_candidates,
            timeline,
        )
        for module in modules:
            tree = ast.parse(inspect.getsource(module))
            imported = {
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
            }
            self.assertTrue(forbidden.isdisjoint(imported), module.__name__)


if __name__ == "__main__":
    unittest.main()
