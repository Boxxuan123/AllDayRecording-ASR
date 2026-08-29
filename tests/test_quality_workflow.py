from __future__ import annotations

import json
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from allday_asr.audio.tools import AudioMetadata
from allday_asr.services import quality_workflow as workflow
from allday_asr.services.quality_asr import QualityAsrSettings
from allday_asr.services.quality_diarization import QualityDiarizationSettings
from allday_asr.services.semantic_v2e02 import SemanticV2E02Settings
from allday_asr.services.session_ingest import (
    CANONICAL_MANIFEST_FORMAT,
    ingest_session_manifest,
)
from allday_asr.storage.database import Database


class QualityWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parent / f"quality-workflow-{uuid4().hex}"
        self.root.mkdir()
        self.database = Database(self.root / "state.sqlite3")

    def tearDown(self) -> None:
        for path in self.root.iterdir():
            path.unlink(missing_ok=True)
        self.root.rmdir()

    def test_workflow_persists_semantic_ready_empty_without_cloud_stage(self) -> None:
        session_id, _ = self._import_session("empty-workflow")
        semantic_mock = patch.object(workflow, "run_semantic_v2e02").start()
        self.addCleanup(patch.stopall)
        with (
            patch.object(
                workflow,
                "run_quality_asr",
                return_value=SimpleNamespace(run_id=101),
            ),
            patch.object(
                workflow,
                "run_quality_diarization",
                return_value=SimpleNamespace(run_id=102),
            ),
            patch.object(
                self.database,
                "list_committed_asr_tokens",
                return_value=[],
            ),
        ):
            summary = self._run(session_id)

        self.assertEqual(summary.state, "semantic_ready_empty")
        self.assertIsNone(summary.semantic_run_id)
        semantic_mock.assert_not_called()
        run = self.database.get_processing_run(summary.workflow_run_id)
        self.assertEqual(run["status"], "completed")
        persisted = json.loads(str(run["summary_json"]))
        self.assertEqual(persisted["workflow_state"], "semantic_ready_empty")
        self.assertEqual(persisted["integrity"]["instances"][0]["status"], "verified")

    def test_workflow_runs_local_semantic_stage_when_asr_has_tokens(self) -> None:
        session_id, _ = self._import_session("semantic-workflow")
        with (
            patch.object(
                workflow,
                "run_quality_asr",
                return_value=SimpleNamespace(run_id=201),
            ),
            patch.object(
                workflow,
                "run_quality_diarization",
                return_value=SimpleNamespace(run_id=202),
            ),
            patch.object(
                self.database,
                "list_committed_asr_tokens",
                return_value=[{"text": "测试"}],
            ),
            patch.object(
                workflow,
                "run_semantic_v2e02",
                return_value=SimpleNamespace(run_id=203),
            ) as semantic_mock,
        ):
            summary = self._run(session_id)

        self.assertEqual(summary.state, "semantic_ready")
        self.assertEqual(summary.semantic_run_id, 203)
        semantic_mock.assert_called_once()

    def test_changed_original_is_detected_before_any_model_stage(self) -> None:
        session_id, audio_path = self._import_session("tampered-workflow")
        with audio_path.open("ab") as handle:
            handle.write(b"changed")
        with (
            patch.object(workflow, "run_quality_asr") as asr_mock,
            patch.object(workflow, "run_quality_diarization") as diarization_mock,
            self.assertRaisesRegex(RuntimeError, "完整性校验失败"),
        ):
            self._run(session_id)

        asr_mock.assert_not_called()
        diarization_mock.assert_not_called()
        runs = self.database.list_session_processing_runs(session_id)
        workflow_run = [row for row in runs if row["run_kind"] == "quality_workflow_v2"][-1]
        self.assertEqual(workflow_run["status"], "failed")
        instance = self.database.list_source_instances(session_id)[0]
        self.assertEqual(instance["integrity_status"], "mismatch")

    def test_completed_matching_stages_are_reused(self) -> None:
        session_id, _ = self._import_session("reuse-workflow")
        asr_settings = QualityAsrSettings(model_signature="synthetic-asr")
        diarization_settings = QualityDiarizationSettings(
            model_signature="synthetic-diarization"
        )
        asr_run_id = self.database.start_processing_run(
            None,
            session_id=session_id,
            run_kind="quality_asr_v2c",
            config=asr_settings.to_dict(),
            config_sha256=asr_settings.sha256(),
            pipeline_version="v2-c",
        )
        self.database.finish_processing_run(asr_run_id, status="completed")
        diarization_config = {
            **diarization_settings.to_dict(),
            "asr_run_id": asr_run_id,
        }
        diarization_run_id = self.database.start_processing_run(
            None,
            session_id=session_id,
            run_kind="quality_diarization_v2d",
            config=diarization_config,
            config_sha256="synthetic-diarization-config",
            pipeline_version="v2-d",
            parent_run_id=asr_run_id,
        )
        self.database.finish_processing_run(diarization_run_id, status="completed")

        with (
            patch.object(workflow, "run_quality_asr") as asr_mock,
            patch.object(workflow, "run_quality_diarization") as diarization_mock,
        ):
            summary = self._run(session_id)

        self.assertEqual(summary.asr_run_id, asr_run_id)
        self.assertEqual(summary.diarization_run_id, diarization_run_id)
        self.assertEqual(summary.reused_stages, ("asr", "diarization"))
        asr_mock.assert_not_called()
        diarization_mock.assert_not_called()

    def _run(self, session_id: int):
        return workflow.run_quality_workflow(
            self.database,
            None,
            session_id=session_id,
            asr_settings=QualityAsrSettings(model_signature="synthetic-asr"),
            diarization_settings=QualityDiarizationSettings(
                model_signature="synthetic-diarization"
            ),
            semantic_settings=SemanticV2E02Settings(),
            primary_factory=lambda: object(),
            secondary_factory=lambda: object(),
            diarization_factory=lambda: object(),
        )

    def _import_session(self, session_key: str) -> tuple[int, Path]:
        audio_path = self.root / f"{session_key}.wav"
        with wave.open(str(audio_path), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16_000)
            writer.writeframes(b"\0\0" * 1_600)
        manifest_path = self.root / f"{session_key}.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "format": CANONICAL_MANIFEST_FORMAT,
                    "sessionKey": session_key,
                    "sessionStartedAt": "2026-08-29T01:00:00+00:00",
                    "device": "synthetic-watch",
                    "timezone": "Asia/Singapore",
                    "audio": {
                        "sampleRate": 16_000,
                        "channels": 1,
                        "bitsPerSample": 16,
                    },
                    "chunks": [
                        {
                            "index": 0,
                            "fileName": audio_path.name,
                            "firstSample": 0,
                            "sampleCount": 1_600,
                        }
                    ],
                    "continuityValid": True,
                }
            ),
            encoding="utf-8",
        )
        metadata = AudioMetadata(
            duration_ms=100,
            codec="pcm_s16le",
            sample_rate=16_000,
            channels=1,
            bit_rate=256_000,
            recorded_at="2026-08-29T01:00:00+00:00",
            encoder=None,
        )
        with patch(
            "allday_asr.services.session_ingest.probe_audio",
            return_value=metadata,
        ):
            summary = ingest_session_manifest(self.database, manifest_path)
        return summary.session_id, audio_path


if __name__ == "__main__":
    unittest.main()
