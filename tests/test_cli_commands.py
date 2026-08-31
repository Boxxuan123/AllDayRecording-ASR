from __future__ import annotations

import ast
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import typer
from typer.testing import CliRunner

from allday_asr.cli import app
from allday_asr.interfaces.cli.targets import resolve_v2_target


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "allday_asr"
COMMANDS_ROOT = PACKAGE_ROOT / "interfaces" / "cli" / "commands"
CLI_PATH = PACKAGE_ROOT / "cli.py"


class CliCommandModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_v2_command_groups_live_under_explicit_legacy_entry(self) -> None:
        cases = {
            "session": ("import-manifest", "backup-verify", "readiness"),
            "workflow": ("run", "status"),
            "asr": ("run", "status", "snapshot"),
            "diarization": (
                "run",
                "refine",
                "identity-audit",
                "mine-identities",
            ),
            "semantic": ("build", "export", "replay", "status"),
        }
        for group, command_names in cases.items():
            result = self.runner.invoke(app, ["legacy", group, "--help"])
            self.assertEqual(result.exit_code, 0, result.output)
            for command_name in command_names:
                self.assertIn(command_name, result.output, group)

    def test_run_options_remain_visible_after_command_split(self) -> None:
        cases = {
            "device": (
                "--auto-workflow",
                "--workflow-shadow",
                "--v3",
            ),
            "workflow": ("--session", "--shadow", "--profile"),
            "asr": ("--session", "--max-windows", "--resume-run-id"),
            "diarization": ("--session", "--asr-run", "--model-path"),
            "semantic": ("--session", "--episode-gap-seconds"),
        }
        for group, options in cases.items():
            command = {
                "semantic": "build",
                "device": "receive",
            }.get(group, "run")
            prefix = [] if group == "device" else ["legacy"]
            result = self.runner.invoke(app, [*prefix, group, command, "--help"])
            self.assertEqual(result.exit_code, 0, result.output)
            for option in options:
                self.assertIn(option, result.output, group)

    def test_remaining_command_groups_and_root_commands_keep_names(self) -> None:
        groups = {
            "benchmark": (
                "migrate-v1-truth",
                "init-blind",
                "oracle-asr",
                "compare-oracle-pair",
            ),
            "evaluation": ("init", "run"),
            "voice-library": ("sync", "status", "accumulate", "enroll-person"),
        }
        for group, command_names in groups.items():
            result = self.runner.invoke(app, ["legacy", group, "--help"])
            self.assertEqual(result.exit_code, 0, result.output)
            for command_name in command_names:
                self.assertIn(command_name, result.output, group)

        root = self.runner.invoke(app, ["--help"])
        self.assertEqual(root.exit_code, 0, root.output)
        for command_name in (
            "web",
            "desktop",
            "status",
            "migrate",
            "legacy-import",
            "release-prepare",
            "release-verify",
            "worker",
            "device",
            "legacy",
        ):
            self.assertIn(command_name, root.output)
        for legacy_only in ("daily-run", "ingest", "recordings", "timeline"):
            self.assertNotIn(legacy_only, root.output)

        legacy = self.runner.invoke(app, ["legacy", "--help"])
        self.assertEqual(legacy.exit_code, 0, legacy.output)
        for command_name in (
            "web",
            "config-show",
            "daily-run",
            "doctor",
            "ingest",
            "recordings",
            "process",
            "timeline",
            "export",
            "clip",
        ):
            self.assertIn(command_name, legacy.output)

    def test_root_cli_no_longer_defines_extracted_commands(self) -> None:
        tree = ast.parse(CLI_PATH.read_text(encoding="utf-8"), filename=str(CLI_PATH))
        definitions = {
            node.name for node in tree.body if isinstance(node, ast.FunctionDef)
        }
        extracted = {
            "quality_asr_run",
            "quality_asr_snapshot",
            "quality_diarization_run",
            "quality_workflow_run",
            "semantic_v2_build",
            "session_import_manifest",
        }
        self.assertTrue(extracted.isdisjoint(definitions))
        self.assertEqual(definitions, set())
        imported_modules = {
            node.module
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module
        }
        self.assertEqual(imported_modules, {"allday_asr.interfaces.cli.app"})

    def test_each_command_group_imports_only_its_service_family(self) -> None:
        expected = {
            "sessions.py": {
                "allday_asr.services.session_backup",
                "allday_asr.services.session_ingest",
                "allday_asr.services.session_readiness",
            },
            "workflow.py": {"allday_asr.services.quality_workflow"},
            "asr.py": {"allday_asr.services.quality_asr"},
            "diarization.py": {"allday_asr.services.evaluation"},
            "semantic.py": set(),
            "utility.py": {
                "allday_asr.services.daily",
                "allday_asr.services.sources",
            },
            "evaluation.py": {"allday_asr.services.evaluation"},
            "benchmark.py": {
                "allday_asr.services.benchmark",
                "allday_asr.services.evaluation",
            },
            "library.py": {
                "allday_asr.services.enrollment",
                "allday_asr.services.voice_library",
            },
            "legacy.py": {
                "allday_asr.services.diarization",
                "allday_asr.services.enrollment",
                "allday_asr.services.ingest",
                "allday_asr.services.processing",
                "allday_asr.services.review",
                "allday_asr.services.speakers",
                "allday_asr.services.timeline",
                "allday_asr.services.verification",
            },
        }
        for filename, service_imports in expected.items():
            path = COMMANDS_ROOT / filename
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            actual = {
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                and (node.module or "").startswith("allday_asr.services.")
            }
            self.assertEqual(actual, service_imports, filename)

    def test_shared_target_resolution_preserves_validation(self) -> None:
        database = MagicMock()
        with self.assertRaisesRegex(typer.BadParameter, "必须提供 recording_id"):
            resolve_v2_target(database, None, None)

        database.get_session_for_recording.return_value = {"id": 7}
        self.assertEqual(resolve_v2_target(database, 3, None), (3, 7))

        database.get_recording_session.return_value = {"legacy_recording_id": 4}
        with self.assertRaisesRegex(typer.BadParameter, "不属于同一会话"):
            resolve_v2_target(database, 3, 9)


if __name__ == "__main__":
    unittest.main()
