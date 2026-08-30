from __future__ import annotations

import ast
import inspect
import unittest
from unittest.mock import MagicMock, patch

import allday_asr.application.semantic.current as current_semantic
import allday_asr.application.semantic.legacy.v2e0 as semantic_v2e0
import allday_asr.application.semantic.legacy.v2e01 as semantic_v2e01
import allday_asr.services.semantic_v2e0 as semantic_v2e0_compat
import allday_asr.services.semantic_v2e01 as semantic_v2e01_compat
import allday_asr.services.semantic_v2e02 as semantic_v2e02_compat
from allday_asr.application.semantic.contracts import (
    DEFAULT_SEMANTIC_VERSION,
    SemanticVersion,
    require_semantic_version,
)
from allday_asr.application.semantic.evidence import (
    ConversationEvidenceSettings,
    build_conversation_evidence,
)
from allday_asr.application.semantic.overview import overview
from allday_asr.application.semantic.pipeline import run
from allday_asr.application.semantic.versions import overview_for, runner_for
from allday_asr.services.semantic_v2e01 import (
    SemanticV2E01Settings,
    build_conversation_evidence as legacy_build_conversation_evidence,
)


class SemanticVersionDispatchTests(unittest.TestCase):
    def test_default_and_historical_versions_have_explicit_dispatch(self) -> None:
        self.assertEqual(DEFAULT_SEMANTIC_VERSION, SemanticVersion.V2_E_0_2)
        self.assertEqual(
            runner_for("v2-e.0").__name__,
            "run_semantic_v2e0",
        )
        self.assertEqual(
            runner_for("v2-e.0.1").__name__,
            "run_semantic_v2e01",
        )
        self.assertEqual(
            runner_for("v2-e.0.2").__name__,
            "run_semantic_v2e02",
        )
        self.assertEqual(
            overview_for("v2-e.0.2").__name__,
            "semantic_overview",
        )

    def test_unknown_version_fails_without_silent_fallback(self) -> None:
        with self.assertRaisesRegex(ValueError, "不支持的 semantic"):
            require_semantic_version("v2-e.future")
        with self.assertRaisesRegex(ValueError, "不支持的 semantic"):
            runner_for("v2-e.future")

        database = MagicMock()
        database.get_session_for_recording.return_value = {"id": 5}
        database.list_session_processing_runs.return_value = [
            {
                "run_kind": "semantic_v2e0",
                "status": "completed",
                "pipeline_version": "v2-e.future",
            }
        ]
        with self.assertRaisesRegex(ValueError, "不支持的 semantic"):
            overview(database, 2)

        database.list_session_processing_runs.return_value = [
            {
                "run_kind": "semantic_v2e0",
                "status": "completed",
                "pipeline_version": "v2-e.0.1",
            }
        ]
        with self.assertRaisesRegex(ValueError, "不能按 v2-e.0 展示"):
            overview(database, 2, version="v2-e.0")

    def test_stable_pipeline_uses_one_current_default(self) -> None:
        database = MagicMock()
        dispatched = MagicMock(return_value={"run_id": 9})
        with patch(
            "allday_asr.application.semantic.pipeline.runner_for",
            return_value=dispatched,
        ) as select:
            result = run(database, 3, session_id=4)
        select.assert_called_once_with(SemanticVersion.V2_E_0_2)
        dispatched.assert_called_once_with(database, 3, session_id=4)
        self.assertEqual(result, {"run_id": 9})

        with self.assertRaisesRegex(ValueError, "legacy recording_id"):
            run(database, None, version="v2-e.0.1")

    def test_current_module_no_longer_imports_older_main_flows(self) -> None:
        tree = ast.parse(inspect.getsource(current_semantic))
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        self.assertNotIn(
            "allday_asr.services.semantic_v2e0",
            imported_modules,
        )
        self.assertNotIn(
            "allday_asr.services.semantic_v2e01",
            imported_modules,
        )

    def test_legacy_service_paths_are_thin_compatibility_exports(self) -> None:
        self.assertIs(semantic_v2e0_compat.run_semantic_v2e0, semantic_v2e0.run_semantic_v2e0)
        self.assertIs(
            semantic_v2e01_compat.run_semantic_v2e01,
            semantic_v2e01.run_semantic_v2e01,
        )
        self.assertIs(
            semantic_v2e02_compat.run_semantic_v2e02,
            current_semantic.run_semantic_v2e02,
        )


class SharedSemanticEvidenceTests(unittest.TestCase):
    def test_shared_conversation_seed_preserves_v2e01_fixture(self) -> None:
        tokens = [
            {
                "id": 1,
                "text": "明天",
                "start_ms": 1_000,
                "end_ms": 1_500,
                "speaker": "SPEAKER_00",
                "speaker_kind": "primary",
                "has_overlap": False,
                "source_refs": [
                    {
                        "source_object_id": 11,
                        "source_instance_id": 12,
                        "source_sha256": "source-a",
                        "source_start_ms": 1_000,
                        "source_end_ms": 1_500,
                    }
                ],
            },
            {
                "id": 2,
                "text": "十点见",
                "start_ms": 1_600,
                "end_ms": 2_100,
                "speaker": "SPEAKER_00",
                "speaker_kind": "primary",
                "has_overlap": False,
                "source_refs": [
                    {
                        "source_object_id": 11,
                        "source_instance_id": 12,
                        "source_sha256": "source-a",
                        "source_start_ms": 1_600,
                        "source_end_ms": 2_100,
                    }
                ],
            },
        ]
        shared = build_conversation_evidence(
            tokens,
            [],
            settings=ConversationEvidenceSettings(),
        )
        legacy = legacy_build_conversation_evidence(
            tokens,
            [],
            settings=SemanticV2E01Settings(),
        )
        self.assertEqual(shared, legacy)


if __name__ == "__main__":
    unittest.main()
