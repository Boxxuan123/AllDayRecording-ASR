from __future__ import annotations

import json
import hashlib
import os
import unittest
from pathlib import Path
from typing import Any

from allday_asr.v3 import CONTRACT_VERSION, PROJECTION_VERSION
from allday_asr.v3.paths import PROJECT_ROOT
from allday_asr.v3.adapters.sqlite.migrations import MIGRATIONS
from allday_asr.v3.contracts import (
    UTTERANCE_DTO_SCHEMA,
    validate_reminder_dto,
    validate_utterance_dto,
)
from allday_asr.v3.contracts.decoders import KNOWN_ENUMS, decode_contract_fixture


CONTRACT_ROOT = PROJECT_ROOT / "contracts" / "v3"


class V3ContractTests(unittest.TestCase):
    def test_manifest_is_the_only_versioned_contract_index(self) -> None:
        manifest = _read_json(CONTRACT_ROOT / "manifest.json")
        self.assertEqual(manifest["contract_version"], CONTRACT_VERSION)
        self.assertEqual(manifest["projection_version"], PROJECTION_VERSION)
        self.assertEqual(manifest["openapi_version"], "3.1.0")

        entries = [
            manifest["release_lock"],
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

        release = _read_json(CONTRACT_ROOT / "release-lock.json")
        self.assertTrue(release["frozen"])
        self.assertEqual(release["release_version"], CONTRACT_VERSION)
        self.assertEqual(release["contract_version"], CONTRACT_VERSION)
        self.assertEqual(release["projection_version"], PROJECTION_VERSION)
        self.assertEqual(
            release["core_schema_version"], max(item.version for item in MIGRATIONS)
        )
        self.assertEqual(release["phone_projection_schema_version"], 7)
        self.assertEqual(
            release["default_entries"],
            {"desktop": "v3", "phone": "v3", "legacy": "read_only"},
        )

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
            ("reminder", "event_id"),
        ):
            self.assertRegex(
                fixture[resource_name][id_field],
                r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab]",
            )
        self.assertRegex(fixture["audio_asset"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("path", fixture["audio_asset"])
        self.assertEqual(validate_reminder_dto(fixture["reminder"]), fixture["reminder"])

    def test_utterance_dto_is_canonical_and_runtime_validated(self) -> None:
        schema = _read_json(CONTRACT_ROOT / "schemas" / "utterance.schema.json")
        fixture = _read_json(CONTRACT_ROOT / "fixtures" / "core-resources.json")
        utterance = fixture["utterance"]
        changed = fixture["device_sync_response"]["changes"][0]["resource"]

        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(UTTERANCE_DTO_SCHEMA["required"]))
        self.assertEqual(
            set(schema["properties"]), set(UTTERANCE_DTO_SCHEMA["properties"])
        )
        self.assertEqual(
            schema["properties"]["status"],
            UTTERANCE_DTO_SCHEMA["properties"]["status"],
        )
        self.assertEqual(validate_utterance_dto(utterance), utterance)
        self.assertEqual(validate_utterance_dto(changed), changed)

        invalid_values = []
        for name, replacement in (
            ("missing speaker_label", {key: value for key, value in utterance.items() if key != "speaker_label"}),
            ("reversed range", {**utterance, "end_ms": utterance["start_ms"]}),
            ("unknown status", {**utterance, "status": "future"}),
            ("invalid id", {**utterance, "utterance_id": "utterance-1"}),
            ("extra field", {**utterance, "run_id": "not-on-wire"}),
        ):
            invalid_values.append((name, replacement))
        for name, value in invalid_values:
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_utterance_dto(value)

        desktop = _read_json(
            CONTRACT_ROOT / "openapi" / "desktop-api.openapi.json"
        )
        session_utterance = desktop["components"]["schemas"]["SessionDetail"][
            "properties"
        ]["utterances"]["items"]
        correction = desktop["paths"][
            "/api/v3/utterances/{utterance_id}/corrections"
        ]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(
            session_utterance, {"$ref": "../schemas/utterance.schema.json"}
        )
        self.assertEqual(correction, session_utterance)

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

        knowledge_paths = {
            "/api/v3/evidence-spans",
            "/api/v3/events",
            "/api/v3/events/{event_id}/operations",
            "/api/v3/memories",
            "/api/v3/knowledge-generations",
            "/api/v3/knowledge-proposals",
            "/api/v3/knowledge-proposals/{proposal_id}/accept",
            "/api/v3/knowledge-proposals/{proposal_id}/reject",
            "/api/v3/derivations/affected",
            "/api/v3/invalidations",
            "/api/v3/recompute-requests",
        }
        self.assertTrue(knowledge_paths.issubset(desktop_paths))
        self.assertTrue(knowledge_paths.isdisjoint(device_paths))
        reminder_paths = {
            "/api/v3/reminder-generations",
            "/api/v3/reminder-generations/codex",
            "/api/v3/reminder-candidates",
            "/api/v3/reminder-candidates/{candidate_id}",
            "/api/v3/reminder-candidates/{candidate_id}/feedback",
            "/api/v3/reminder-candidates/{candidate_id}/confirm",
            "/api/v3/reminder-candidates/{candidate_id}/modify",
            "/api/v3/reminder-candidates/{candidate_id}/ignore",
            "/api/v3/reminders",
            "/api/v3/reminders/due",
            "/api/v3/reminders/{event_id}/deliver",
        }
        self.assertTrue(reminder_paths.issubset(desktop_paths))
        self.assertTrue(reminder_paths.isdisjoint(device_paths))
        people_paths = {
            "/api/v3/persons",
            "/api/v3/persons/{person_id}",
            "/api/v3/persons/{person_id}/profile",
            "/api/v3/persons/{person_id}/identity-policy",
            "/api/v3/persons/{person_id}/memories",
            "/api/v3/persons/{person_id}/memories/refresh",
            "/api/v3/person-memories/{memory_id}/revise",
            "/api/v3/person-memories/{memory_id}/expire",
            "/api/v3/person-memories/{memory_id}/retract",
            "/api/v3/person-memories/{memory_id}/undo",
            "/api/v3/speaker-cluster-runs",
            "/api/v3/speaker-clusters",
            "/api/v3/speaker-clusters/rematch",
            "/api/v3/speaker-clusters/{cluster_id}",
            "/api/v3/speaker-clusters/{cluster_id}/label",
            "/api/v3/speaker-clusters/{cluster_id}/merge",
            "/api/v3/speaker-clusters/{cluster_id}/split",
            "/api/v3/speaker-clusters/{cluster_id}/ignore",
            "/api/v3/speaker-clusters/{cluster_id}/undo",
            "/api/v3/voice-prototype-candidates",
            "/api/v3/voice-prototypes/{prototype_id}/reviews",
        }
        self.assertTrue(people_paths.issubset(desktop_paths))
        self.assertTrue(people_paths.isdisjoint(device_paths))
        insight_paths = {
            "/api/v3/daily-summaries",
            "/api/v3/daily-summaries/generate",
            "/api/v3/daily-summaries/{summary_date}",
            "/api/v3/relationship-observations",
            "/api/v3/relationship-observations/generate",
            "/api/v3/relationship-observations/{report_id}",
            "/api/v3/relationship-observations/{report_id}/revise",
            "/api/v3/relationship-observations/{report_id}/retract",
            "/api/v3/relationship-observations/{report_id}/undo",
        }
        self.assertTrue(insight_paths.issubset(desktop_paths))
        self.assertTrue(insight_paths.isdisjoint(device_paths))
        knowledge = _read_json(
            CONTRACT_ROOT / "schemas" / "knowledge.schema.json"
        )["$defs"]
        self.assertTrue(
            {
                "EvidenceSpan",
                "EventState",
                "EventOperation",
                "MemoryRecord",
                "GenerationSubmission",
                "ProposalResolution",
            }.issubset(knowledge)
        )
        reminder = _read_json(
            CONTRACT_ROOT / "schemas" / "reminder.schema.json"
        )
        self.assertFalse(reminder["additionalProperties"])
        self.assertEqual(
            reminder["$defs"]["ReminderOperation"]["enum"],
            [
                "CREATE_TASK",
                "CREATE_APPOINTMENT",
                "UPDATE_EVENT",
                "CANCEL_EVENT",
                "MARK_DONE",
                "IGNORE",
            ],
        )
        person = _read_json(CONTRACT_ROOT / "schemas" / "person.schema.json")
        self.assertEqual(
            person["$defs"]["ClusterStatus"]["enum"],
            ["active", "merged", "split", "ignored"],
        )
        self.assertNotIn("vector", person["$defs"]["SpeakerPrototype"]["properties"])
        person_memory = _read_json(
            CONTRACT_ROOT / "schemas" / "person-memory.schema.json"
        )
        self.assertEqual(
            person_memory["$defs"]["MemoryKind"]["enum"],
            [
                "stable_fact",
                "preference",
                "short_term_state",
                "plan",
                "commitment",
                "model_observation",
            ],
        )
        serialized_memory = json.dumps(person_memory, sort_keys=True).lower()
        self.assertNotIn("local_path", serialized_memory)
        self.assertIn("confirmation_status", serialized_memory)
        self.assertIn("valid_until", serialized_memory)
        insight = _read_json(
            CONTRACT_ROOT / "schemas" / "insight.schema.json"
        )["$defs"]
        self.assertEqual(
            insight["RelationshipObservation"]["properties"]["window_days"]["enum"],
            [7, 30],
        )
        self.assertEqual(
            set(insight["DailySummary"]["properties"]["narrative"]["required"]),
            {
                "what_happened", "decisions", "new_todos", "completed",
                "unresolved", "important_people_interactions",
                "memorable_quotes", "tomorrow_attention",
            },
        )

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
