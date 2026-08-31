from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from typer.testing import CliRunner

from allday_asr.cli import app
from allday_asr.v3.bootstrap import start_empty_runtime
from allday_asr.v3.adapters.sqlite.migration_runner import (
    LATEST_V3_SCHEMA_VERSION,
)
from allday_asr.v3.config import (
    DeploymentMode,
    V3ConfigurationError,
    V3Settings,
)


class V3BootstrapTests(unittest.TestCase):
    def test_v3_is_enabled_by_default_after_release_cutover(self) -> None:
        settings = V3Settings.from_environment({})
        self.assertTrue(settings.enabled)
        self.assertEqual(start_empty_runtime(settings).state, "empty_ready")

    def test_empty_runtime_does_not_open_or_modify_a_v2_database(self) -> None:
        directory = Path(__file__).parent / f"v3-empty-{uuid4().hex}"
        directory.mkdir()
        marker = directory / "v2.sqlite3"
        try:
            marker.write_bytes(b"immutable-v2-marker")
            before = marker.read_bytes()

            runtime = start_empty_runtime(V3Settings(enabled=True))

            self.assertEqual(runtime.state, "empty_ready")
            self.assertEqual(marker.read_bytes(), before)
            self.assertEqual(list(directory.iterdir()), [marker])
        finally:
            marker.unlink(missing_ok=True)
            directory.rmdir()

    def test_production_rejects_placeholder_or_missing_passkey_values(self) -> None:
        for rp_id in (
            None,
            "localhost",
            "alldayrecording.local",
            "example.com",
            "192.168.1.20",
        ):
            with self.subTest(rp_id=rp_id), self.assertRaises(
                V3ConfigurationError
            ):
                V3Settings(
                    enabled=True,
                    deployment_mode=DeploymentMode.PRODUCTION,
                    passkey_rp_id=rp_id,
                    passkey_origins=("ohos:app-id:real-app-id",),
                ).validate()

    def test_production_accepts_injected_rp_id_and_ohos_origin(self) -> None:
        settings = V3Settings.from_environment(
            {
                "ALLDAY_V3_ENABLED": "1",
                "ALLDAY_V3_DEPLOYMENT": "production",
                "ALLDAY_V3_PASSKEY_RP_ID": "passkeys.alldayrecording.app",
                "ALLDAY_V3_PASSKEY_ORIGINS": "ohos:app-id:bound-production-app",
            }
        )
        self.assertTrue(settings.enabled)
        self.assertEqual(settings.deployment_mode, DeploymentMode.PRODUCTION)

    def test_cli_empty_start_is_explicit_and_machine_readable(self) -> None:
        result = CliRunner().invoke(
            app,
            ["start"],
            env={"ALLDAY_V3_ENABLED": "1"},
        )
        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["state"], "empty_ready")
        self.assertEqual(payload["contract_version"], "3.0.0")

    def test_cli_migration_requires_feature_flag_and_uses_v3_only_state(self) -> None:
        directory = Path(__file__).parent / f"v3-migrate-{uuid4().hex}"
        try:
            disabled = CliRunner().invoke(
                app,
                ["migrate", "--state-dir", str(directory)],
                env={"ALLDAY_V3_ENABLED": "0"},
            )
            self.assertNotEqual(disabled.exit_code, 0)
            self.assertFalse(directory.exists())

            enabled = CliRunner().invoke(
                app,
                ["migrate", "--state-dir", str(directory)],
                env={"ALLDAY_V3_ENABLED": "1"},
            )
            self.assertEqual(enabled.exit_code, 0, enabled.output)
            payload = json.loads(enabled.output)
            self.assertEqual(
                payload["schema_version"], LATEST_V3_SCHEMA_VERSION
            )
            self.assertEqual(payload["state"], "ready")
            self.assertTrue((directory / "core.sqlite3").is_file())
        finally:
            shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
