from __future__ import annotations

import ast
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from allday_asr.config import AppConfig
from allday_asr.interfaces.cli.runtime import (
    RuntimeBackendBuilders,
    build_quality_asr_runtime,
    build_quality_diarization_runtime,
    runtime_signature,
)
from allday_asr.paths import AppPaths


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = PROJECT_ROOT / "src" / "allday_asr" / "cli.py"
RUNTIME_PATH = (
    PROJECT_ROOT / "src" / "allday_asr" / "interfaces" / "cli" / "runtime.py"
)
BENCHMARK_COMMAND_PATH = (
    PROJECT_ROOT
    / "src"
    / "allday_asr"
    / "interfaces"
    / "cli"
    / "commands"
    / "benchmark.py"
)


class CliRuntimeCompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.speech_gate = MagicMock(return_value={"kind": "speech-gate"})
        self.primary = MagicMock(return_value={"kind": "primary"})
        self.secondary = MagicMock(return_value={"kind": "secondary"})
        self.diarization = MagicMock(return_value={"kind": "diarization"})
        self.builders = RuntimeBackendBuilders(
            speech_gate=self.speech_gate,
            primary_asr=self.primary,
            secondary_asr=self.secondary,
            diarization=self.diarization,
        )

    def test_asr_runtime_uses_injected_backend_builders(self) -> None:
        config = AppConfig()
        profile_resolver = MagicMock(return_value="compatible-8gb")

        runtime = build_quality_asr_runtime(
            config,
            requested_profile="compatible-8gb",
            max_windows=2,
            builders=self.builders,
            profile_resolver=profile_resolver,
        )

        profile_resolver.assert_called_once_with(
            "compatible-8gb", device=config.runtime.device
        )
        self.assertEqual(runtime.settings.vram_profile, "compatible-8gb")
        self.assertEqual(runtime.settings.max_windows, 2)
        self.assertEqual(len(runtime.settings.model_signature), 64)
        self.speech_gate.assert_called_once()
        self.assertEqual(runtime.primary_factory(), {"kind": "primary"})
        self.assertEqual(runtime.secondary_factory(), {"kind": "secondary"})
        self.primary.assert_called_once_with(
            model_id=config.asr.primary_model,
            aligner_model_id=config.asr.forced_aligner_model,
            device=config.runtime.device,
            batch_size=config.asr.primary_batch_size_8gb,
            max_new_tokens=config.asr.max_new_tokens,
            speech_gate={"kind": "speech-gate"},
        )
        self.secondary.assert_called_once_with(
            model_id=config.asr.secondary_model,
            device=config.runtime.device,
        )

    def test_diarization_runtime_uses_injected_backend_builder(self) -> None:
        config = AppConfig()
        model_path = PROJECT_ROOT / "models" / "synthetic-community"

        runtime = build_quality_diarization_runtime(
            config,
            model_path=model_path,
            num_speakers=3,
            builders=self.builders,
        )

        self.assertEqual(runtime.settings.num_speakers, 3)
        self.assertEqual(len(runtime.settings.model_signature), 64)
        self.assertEqual(runtime.backend_factory(), {"kind": "diarization"})
        self.diarization.assert_called_once_with(
            model_id=config.quality_diarization.model_id,
            model_path=model_path,
            device=config.runtime.device,
            token_env=config.quality_diarization.token_env,
        )

    def test_runtime_signature_is_canonical(self) -> None:
        self.assertEqual(
            runtime_signature({"b": 2, "a": 1}),
            runtime_signature({"a": 1, "b": 2}),
        )

    def test_explicit_paths_are_forwarded_to_model_factories(self) -> None:
        config = AppConfig()
        paths = AppPaths.from_environment({}, project_root=PROJECT_ROOT / "isolated")
        asr_runtime = build_quality_asr_runtime(
            config,
            requested_profile="compatible-8gb",
            builders=self.builders,
            profile_resolver=MagicMock(return_value="compatible-8gb"),
            paths=paths,
        )
        diarization_runtime = build_quality_diarization_runtime(
            config,
            model_path=None,
            builders=self.builders,
            paths=paths,
        )

        asr_runtime.primary_factory()
        asr_runtime.secondary_factory()
        diarization_runtime.backend_factory()

        self.assertIs(self.primary.call_args.kwargs["paths"], paths)
        self.assertIs(self.secondary.call_args.kwargs["paths"], paths)
        self.assertIs(self.diarization.call_args.kwargs["paths"], paths)

    def test_concrete_model_factories_are_not_top_level_cli_imports(self) -> None:
        forbidden = {
            "FunAsrNanoBackend",
            "PyannoteCommunityBackend",
            "Qwen3AsrBackend",
            "SpeechGateSettings",
            "create_oracle_backend",
        }
        for path in (CLI_PATH, RUNTIME_PATH, BENCHMARK_COMMAND_PATH):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported = {
                alias.name
                for node in tree.body
                if isinstance(node, (ast.Import, ast.ImportFrom))
                for alias in node.names
            }
            self.assertTrue(forbidden.isdisjoint(imported), path.name)


if __name__ == "__main__":
    unittest.main()
