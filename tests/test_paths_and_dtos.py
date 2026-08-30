from __future__ import annotations

import os
import shutil
import unittest
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from allday_asr.asr.funasr_backend import FunASRBackend
from allday_asr.asr.oracle_backends import (
    FunAsrNanoOracleBackend,
    QwenOracleBackend,
    SenseVoiceOracleBackend,
)
from allday_asr.asr.quality_backends import FunAsrNanoBackend, Qwen3AsrBackend
from allday_asr.config import ConfigError, load_config
from allday_asr.diarization.quality_backends import PyannoteCommunityBackend
from allday_asr.interfaces.web.presenters import (
    ActionPayload,
    DailyRunPayload,
    DashboardRunPayload,
    ProcessingRunPayload,
    daily_summary_payload,
)
from allday_asr.paths import AppPaths
from allday_asr.services.daily import DailyRunStep, DailyRunSummary

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def isolated_directory():
    parent = PROJECT_ROOT / "tests" / ".tmp-paths-and-dtos"
    root = parent / uuid4().hex
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root)
        try:
            parent.rmdir()
        except OSError:
            pass


class AppPathsTests(unittest.TestCase):
    def test_environment_construction_is_isolated_and_has_no_io(self) -> None:
        with isolated_directory() as root:
            first = AppPaths.from_environment(
                {"ALLDAY_ASR_STATE_DIR": str(root / "first-state")},
                project_root=root,
            )
            second = AppPaths.from_environment(
                {"ALLDAY_ASR_STATE_DIR": str(root / "second-state")},
                project_root=root,
            )

            self.assertEqual(first.state_dir, root / "first-state")
            self.assertEqual(second.state_dir, root / "second-state")
            self.assertEqual(
                first.database_path, root / "first-state" / "allday_asr.sqlite3"
            )
            self.assertFalse(first.state_dir.exists())
            self.assertFalse(first.output_dir.exists())
            self.assertFalse(first.model_dir.exists())

            first.ensure_runtime_dirs()
            self.assertTrue(first.state_dir.is_dir())
            self.assertTrue(first.output_dir.is_dir())
            self.assertTrue(first.model_dir.is_dir())
            self.assertEqual(
                first.recording_output_dir(7),
                first.output_dir / "recording-000007",
            )

    def test_cache_configuration_targets_explicit_mapping(self) -> None:
        with isolated_directory() as root:
            paths = AppPaths.from_environment({}, project_root=root)
            environment: dict[str, str] = {}
            process_values = {
                name: os.environ.get(name) for name in ("MODELSCOPE_CACHE", "HF_HOME")
            }

            configured = paths.configure_model_cache(environment)

            self.assertEqual(
                configured,
                {
                    "MODELSCOPE_CACHE": str(root / "models" / "modelscope"),
                    "HF_HOME": str(root / "models" / "huggingface"),
                },
            )
            self.assertEqual(
                {name: os.environ.get(name) for name in process_values}, process_values
            )

    def test_config_loader_uses_injected_paths(self) -> None:
        with isolated_directory() as root:
            config_path = root / "isolated.toml"
            paths = AppPaths(
                state_dir=root / "state",
                output_dir=root / "outputs",
                model_dir=root / "models",
                config_path=config_path,
                database_path=root / "state" / "test.sqlite3",
            )

            with self.assertRaisesRegex(ConfigError, "isolated.toml"):
                load_config(paths=paths)

    def test_backend_construction_has_no_cache_side_effects(self) -> None:
        with isolated_directory() as root:
            paths = AppPaths.from_environment({}, project_root=root)
            sentinel_environment = {
                "MODELSCOPE_CACHE": "existing-modelscope",
                "HF_HOME": "existing-huggingface",
                "PYANNOTE_METRICS_ENABLED": "existing-metrics",
            }
            with patch.dict(os.environ, sentinel_environment, clear=False):
                before = {
                    name: os.environ.get(name) for name in sentinel_environment
                }
                backends = [
                    FunASRBackend(device="cpu", paths=paths),
                    Qwen3AsrBackend(device="cpu", paths=paths),
                    FunAsrNanoBackend(device="cpu", paths=paths),
                    SenseVoiceOracleBackend(device="cpu", paths=paths),
                    QwenOracleBackend(device="cpu", paths=paths),
                    FunAsrNanoOracleBackend(device="cpu", paths=paths),
                    PyannoteCommunityBackend(device="cpu", paths=paths),
                ]

                self.assertEqual(
                    {name: os.environ.get(name) for name in sentinel_environment},
                    before,
                )
                self.assertFalse(paths.model_dir.exists())
                self.assertTrue(all(backend is not None for backend in backends))


class PresenterDtoTests(unittest.TestCase):
    def test_key_web_payloads_have_static_fields(self) -> None:
        self.assertEqual(
            ProcessingRunPayload.__required_keys__,
            {
                "id",
                "recording_id",
                "session_id",
                "run_kind",
                "status",
                "config_sha256",
                "started_at",
                "completed_at",
                "error",
                "summary",
                "artifacts",
            },
        )
        self.assertIn("available", DashboardRunPayload.__required_keys__)
        self.assertIn("id", DashboardRunPayload.__optional_keys__)
        self.assertIn("source_segment_ids", ActionPayload.__required_keys__)
        self.assertIn("steps", DailyRunPayload.__required_keys__)

    def test_daily_summary_serialization_is_presenter_owned(self) -> None:
        with isolated_directory() as root:
            summary = DailyRunSummary(
                run_id=11,
                recording_id=7,
                status="completed",
                config_sha256="abc",
                steps=[DailyRunStep("process", "completed", "done")],
                review_actions=["review identity"],
                artifacts={},
                manifest_json_path=root / "manifest.json",
                manifest_markdown_path=root / "manifest.md",
            )

            payload = daily_summary_payload(summary)

            self.assertEqual(payload["run_id"], 11)
            self.assertEqual(
                payload["steps"],
                [{"name": "process", "status": "completed", "detail": "done"}],
            )
            self.assertEqual(
                payload["manifest_markdown_path"],
                str((root / "manifest.md").resolve()),
            )


if __name__ == "__main__":
    unittest.main()
