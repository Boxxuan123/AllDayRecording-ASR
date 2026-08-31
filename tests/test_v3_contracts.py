from __future__ import annotations

import json
import hashlib
import os
import unittest
from pathlib import Path
from typing import Any

from allday_asr.paths import PROJECT_ROOT
from allday_asr.v3 import CONTRACT_VERSION, PROJECTION_VERSION
from allday_asr.v3.contracts.decoders import KNOWN_ENUMS, decode_contract_fixture


CONTRACT_ROOT = PROJECT_ROOT / "contracts" / "v3"


class V3ContractTests(unittest.TestCase):
    def test_manifest_is_the_only_versioned_contract_index(self) -> None:
        manifest = _read_json(CONTRACT_ROOT / "manifest.json")
        self.assertEqual(manifest["contract_version"], CONTRACT_VERSION)
        self.assertEqual(manifest["projection_version"], PROJECTION_VERSION)
        self.assertEqual(manifest["openapi_version"], "3.1.0")

        entries = [
            *manifest["schemas"].values(),
            *manifest["apis"].values(),
            *manifest["fixtures"].values(),
        ]
        declared = {entry["path"] for entry in entries}
        actual = {
            path.relative_to(CONTRACT_ROOT).as_posix()
            for path in CONTRACT_ROOT.rglob("*.json")
            if path.name != "manifest.json"
        }
        self.assertEqual(declared, actual)
        for entry in entries:
            digest = hashlib.sha256(
                (CONTRACT_ROOT / entry["path"]).read_bytes()
            ).hexdigest()
            self.assertEqual(entry["sha256"], digest, entry["path"])

    def test_json_documents_parse_and_external_refs_resolve(self) -> None:
        for path in CONTRACT_ROOT.rglob("*.json"):
            payload = _read_json(path)
            self.assertIsInstance(payload, dict, path)
            for reference in _references(payload):
                if reference.startswith("#") or "://" in reference:
                    continue
                target = reference.split("#", 1)[0]
                self.assertTrue((path.parent / target).resolve().is_file(), reference)

    def test_canonical_fixture_has_valid_resource_invariants(self) -> None:
        fixture = _read_json(CONTRACT_ROOT / "fixtures" / "core-resources.json")
        self.assertEqual(fixture["contract_version"], CONTRACT_VERSION)
        for resource_name, id_field in (
            ("recording_session", "session_id"),
            ("audio_asset", "asset_id"),
            ("processing_run", "run_id"),
            ("utterance", "utterance_id"),
        ):
            self.assertRegex(
                fixture[resource_name][id_field],
                r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab]",
            )
        self.assertRegex(fixture["audio_asset"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("path", fixture["audio_asset"])

    def test_python_decodes_both_shared_fixtures(self) -> None:
        for fixture_name in ("core-resources.json", "forward-enums.json"):
            fixture = _read_json(CONTRACT_ROOT / "fixtures" / fixture_name)
            self.assertEqual(
                decode_contract_fixture(fixture),
                fixture["expected_decode"],
                fixture_name,
            )

    def test_generated_python_enum_snapshot_matches_schema(self) -> None:
        primitives = _read_json(
            CONTRACT_ROOT / "schemas" / "primitives.schema.json"
        )["$defs"]
        expected = {
            "recording_session_state": primitives["RecordingSessionState"]["enum"],
            "aggregate_status": primitives["AggregateStatus"]["enum"],
            "processing_status": primitives["ProcessingStatus"]["enum"],
            "receipt_status": primitives["ClientOperationStatus"]["enum"],
            "resource_type": primitives["ResourceType"]["enum"],
            "sync_operation": primitives["SyncOperation"]["enum"],
        }
        self.assertEqual(
            {key: set(value) for key, value in KNOWN_ENUMS.items()},
            {key: set(value) for key, value in expected.items()},
        )

    def test_desktop_and_device_api_boundaries_are_disjoint(self) -> None:
        desktop = _read_json(
            CONTRACT_ROOT / "openapi" / "desktop-api.openapi.json"
        )
        device = _read_json(
            CONTRACT_ROOT / "openapi" / "device-api.openapi.json"
        )
        desktop_paths = set(desktop["paths"])
        device_paths = set(device["paths"])
        self.assertTrue(all(path.startswith("/api/v3/") for path in desktop_paths))
        self.assertTrue(
            all(path.startswith("/device/v3/") for path in device_paths)
        )
        self.assertTrue(desktop_paths.isdisjoint(device_paths))
        self.assertEqual(
            set(desktop["components"]["securitySchemes"]), {"DesktopSession"}
        )
        self.assertEqual(
            set(device["components"]["securitySchemes"]),
            {"DeviceId", "DeviceChallenge", "DeviceSignature"},
        )
        allowed_scopes = {
            "audio.upload",
            "data.sync.read",
            "data.sync.write",
            "device.status",
        }
        serialized = json.dumps(device["paths"], sort_keys=True).lower()
        for forbidden in ("benchmark", "database", "arbitrary_path", "lab"):
            self.assertNotIn(forbidden, serialized)
        scopes = {
            scope
            for item in device["paths"].values()
            for operation in item.values()
            if isinstance(operation, dict)
            for scope in operation.get("x-required-scopes", [])
        }
        self.assertEqual(scopes, allowed_scopes)

    def test_harmony_consumer_receipt_when_checkout_is_available(self) -> None:
        configured = os.environ.get("ALLDAY_HARMONY_REPO")
        harmony_root = (
            Path(configured)
            if configured
            else PROJECT_ROOT.parent.parent
            / "DevEcoStudioProjects"
            / "AllDayRecording"
        )
        receipt_path = harmony_root / "contracts" / "v3" / "source.json"
        if not receipt_path.is_file():
            self.skipTest("Harmony checkout is not available")
        receipt = _read_json(receipt_path)
        manifest_digest = hashlib.sha256(
            (CONTRACT_ROOT / "manifest.json").read_bytes()
        ).hexdigest()
        self.assertEqual(receipt["contract_version"], CONTRACT_VERSION)
        self.assertEqual(receipt["projection_version"], PROJECTION_VERSION)
        self.assertEqual(receipt["canonical_manifest_sha256"], manifest_digest)
        for name, expected_digest in receipt["canonical_fixtures"].items():
            actual_digest = hashlib.sha256(
                (CONTRACT_ROOT / "fixtures" / name).read_bytes()
            ).hexdigest()
            self.assertEqual(expected_digest, actual_digest, name)
        arkts_fixture = (
            harmony_root / "phone" / "src" / "test" / "V3ContractFixture.test.ets"
        ).read_text(encoding="utf-8")
        for expected_digest in receipt["canonical_fixtures"].values():
            self.assertIn(expected_digest, arkts_fixture)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _references(value: Any) -> list[str]:
    references: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "$ref" and isinstance(child, str):
                references.append(child)
            else:
                references.extend(_references(child))
    elif isinstance(value, list):
        for child in value:
            references.extend(_references(child))
    return references


if __name__ == "__main__":
    unittest.main()
