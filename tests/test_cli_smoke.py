from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from typer.testing import CliRunner

from allday_asr.cli import app


class CliSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_root_help_is_available_without_loading_models(self) -> None:
        result = self.runner.invoke(app, ["--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Usage: allday-asr", result.output)
        self.assertIn("release-prepare", result.output)
        self.assertIn("device", result.output)
        self.assertIn("legacy", result.output)

    def test_invalid_parameter_uses_typer_error_exit(self) -> None:
        result = self.runner.invoke(app, ["legacy", "process", "0"])

        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("Invalid value", result.output)
        self.assertIn("recording_id", result.output)

    def test_read_only_command_succeeds_against_an_empty_database(self) -> None:
        database_path = Path(__file__).parent / f"cli-smoke-{uuid4().hex}.sqlite3"
        try:
            result = self.runner.invoke(
                app,
                ["legacy", "recordings", "--db", str(database_path)],
            )

            self.assertEqual(
                result.exit_code,
                0,
                result.output or repr(result.exception),
            )
            self.assertTrue(database_path.is_file())
        finally:
            for suffix in ("", "-shm", "-wal"):
                Path(f"{database_path}{suffix}").unlink(missing_ok=True)

    def test_failed_doctor_check_uses_failure_exit_without_real_checks(self) -> None:
        failed_check = SimpleNamespace(
            name="synthetic-check",
            ok=False,
            detail="synthetic failure",
        )
        with patch(
            "allday_asr.interfaces.cli.commands.legacy.run_checks",
            return_value=[failed_check],
        ):
            result = self.runner.invoke(app, ["legacy", "doctor"])

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("synthetic-check", result.output)
        self.assertIn("synthetic failure", result.output)


if __name__ == "__main__":
    unittest.main()
