from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import unittest
from pathlib import Path
from uuid import uuid4

from allday_asr.v3.application import LegacyImportCommand
from allday_asr.v3.adapters.legacy_v2 import compose_legacy_v2_import
from allday_asr.v3.bootstrap import V3CorePaths, compose_v3_core
from allday_asr.v3.domain.insights import DAILY_NARRATIVE_SECTIONS
from allday_asr.v3.ports.insight_generation import (
    DailyInsightModelResult,
    RelationshipInsightModelResult,
)
from allday_asr.v3.ports.reminder_generation import ReminderModelResult
from allday_asr.interfaces.transfer.store import UploadRecord
from allday_asr.interfaces.transfer.workflow import build_v3_postprocessor


TEST_ROOT = Path(__file__).parent
STAMP = "2026-08-30T18:00:00+00:00"


class V3LegacyImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TEST_ROOT / f"v3b-import-{uuid4().hex}"
        self.directory.mkdir()
        self.v2_database = self.directory / "legacy-v2.sqlite3"
        self.fixture = _create_v2_fixture(self.v2_database)
        self.core = compose_v3_core(
            V3CorePaths.from_state_dir(self.directory / "v3-state")
        )
        self.core.initialize()
        self.import_legacy_v2 = compose_legacy_v2_import(self.core)

    def tearDown(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_repeated_read_only_import_is_idempotent_and_preserves_session_graph(
        self,
    ) -> None:
        source_before = _digest(self.v2_database)

        first = self.import_legacy_v2.execute(LegacyImportCommand(self.v2_database))
        counts_after_first = self._core_counts()
        second = self.import_legacy_v2.execute(LegacyImportCommand(self.v2_database))
        counts_after_second = self._core_counts()

        self.assertEqual(_digest(self.v2_database), source_before)
        self.assertEqual(first.source_database_sha256, source_before)
        self.assertEqual(second.source_database_sha256, source_before)
        self.assertNotEqual(first.import_id, second.import_id)
        self.assertEqual(counts_after_second, counts_after_first)
        self.assertGreater(second.existing["recording_sessions"], 0)
        self.assertGreater(second.existing["audio_assets"], 0)
        self.assertGreater(second.existing["artifacts"], 0)
        self.assertGreater(second.existing["corrections"], 0)
        self.assertEqual(first.created["utterances"], 2)
        self.assertEqual(first.created["events"], 2)
        self.assertEqual(first.created["persons"], 2)
        self.assertEqual(second.existing["utterances"], 2)
        self.assertEqual(second.existing["events"], 2)
        self.assertEqual(second.existing["persons"], 2)

        namespace = source_before[:24]
        session_ref = f"v2:{namespace}:recording_sessions:1"
        with self.core.database.read() as connection:
            session = connection.execute(
                "SELECT * FROM recording_sessions WHERE legacy_ref = ?",
                (session_ref,),
            ).fetchone()
            session_change = connection.execute(
                """
                SELECT payload_json
                FROM change_events
                WHERE resource_type = 'recording_session'
                  AND resource_id = ?
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (session["session_id"],),
            ).fetchone()
            rows = list(
                connection.execute(
                    """
                    SELECT
                        cs.sequence,
                        cs.session_start_ms,
                        cs.session_end_ms,
                        aa.duration_ms,
                        aa.sha256,
                        ar.state
                    FROM capture_segments cs
                    JOIN audio_assets aa ON aa.asset_id = cs.asset_id
                    JOIN audio_replicas ar ON ar.replica_id = cs.replica_id
                    WHERE cs.session_id = ?
                    ORDER BY cs.sequence
                    """,
                    (session["session_id"],),
                )
            )

        self.assertEqual(
            json.loads(str(session_change["payload_json"]))["session_key"],
            "good-session",
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual([int(row["sequence"]) for row in rows], [0, 1])
        self.assertEqual(
            [int(row["duration_ms"]) for row in rows],
            self.fixture["good_durations"],
        )
        self.assertEqual(
            [str(row["sha256"]) for row in rows], self.fixture["good_sha256"]
        )
        self.assertEqual([str(row["state"]) for row in rows], ["available"] * 2)
        self.assertEqual(int(rows[-1]["session_end_ms"]), 3000)

        with self.core.database.read() as connection:
            utterances = connection.execute(
                "SELECT text, identity FROM utterances ORDER BY ordinal"
            ).fetchall()
            people = connection.execute(
                "SELECT display_name, kind FROM persons ORDER BY kind, display_name"
            ).fetchall()
            events = connection.execute(
                "SELECT event_kind, payload_json FROM event_current_states "
                "ORDER BY event_kind"
            ).fetchall()
            summary_count = connection.execute(
                "SELECT COUNT(*) FROM daily_summary_revisions"
            ).fetchone()[0]
        self.assertEqual(
            [tuple(row) for row in utterances],
            [("我会", "self"), ("去的", "not_self")],
        )
        self.assertEqual(
            {tuple(row) for row in people},
            {("我", "self"), ("母亲", "known")},
        )
        self.assertEqual(
            {str(row["event_kind"]) for row in events},
            {"important_experience", "person_fact"},
        )
        self.assertEqual(summary_count, 0)

        with self.core.database.read() as connection:
            evidence_span_count = int(
                connection.execute("SELECT COUNT(*) FROM evidence_spans").fetchone()[0]
            )
            event_actors = {
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT actor FROM event_operations"
                )
            }
            self_person_id = str(
                connection.execute(
                    "SELECT person_id FROM persons WHERE kind = 'self'"
                ).fetchone()[0]
            )
        self.assertEqual(evidence_span_count, 2)
        self.assertEqual(event_actors, {"system:legacy_v2_import"})

        self.core.person_memory.refresh(self_person_id)
        memory = self.core.person_memory.person(self_person_id)["memories"][0]
        quote = next(item for item in memory["evidence"] if item["utterance_id"])
        self.assertEqual(memory["summary"], "本人表示会去。")
        self.assertEqual(memory["confirmation_status"], "unconfirmed")
        self.assertIsNotNone(quote["media_id"])

    def test_reimport_republishes_one_enriched_session_projection(self) -> None:
        result = self.import_legacy_v2.execute(LegacyImportCommand(self.v2_database))
        namespace = result.source_database_sha256[:24]
        session_ref = f"v2:{namespace}:recording_sessions:1"
        with self.core.database.transaction() as connection:
            session_id = str(
                connection.execute(
                    "SELECT session_id FROM recording_sessions WHERE legacy_ref = ?",
                    (session_ref,),
                ).fetchone()[0]
            )
            payload = json.loads(
                str(
                    connection.execute(
                        """
                        SELECT payload_json FROM change_events
                        WHERE resource_type = 'recording_session'
                          AND resource_id = ?
                        ORDER BY sequence DESC LIMIT 1
                        """,
                        (session_id,),
                    ).fetchone()[0]
                )
            )
            payload.pop("session_key")
            connection.execute(
                """
                INSERT INTO change_events (
                    resource_type, resource_id, revision, operation,
                    payload_json, created_at
                ) VALUES ('recording_session', ?, 1, 'upsert', ?, ?)
                """,
                (session_id, json.dumps(payload), STAMP),
            )

        self.import_legacy_v2.execute(LegacyImportCommand(self.v2_database))
        self.import_legacy_v2.execute(LegacyImportCommand(self.v2_database))

        with self.core.database.read() as connection:
            changes = connection.execute(
                """
                SELECT payload_json FROM change_events
                WHERE resource_type = 'recording_session'
                  AND resource_id = ?
                ORDER BY sequence
                """,
                (session_id,),
            ).fetchall()
        self.assertEqual(len(changes), 3)
        self.assertEqual(
            json.loads(str(changes[-1]["payload_json"]))["session_key"],
            "good-session",
        )

    def test_changed_source_at_same_path_reuses_the_import_namespace(self) -> None:
        first = self.import_legacy_v2.execute(LegacyImportCommand(self.v2_database))
        first_namespace = first.source_database_sha256[:24]
        with sqlite3.connect(self.v2_database) as connection:
            connection.execute(
                "UPDATE unknown_legacy_objects SET payload = ? WHERE id = 1",
                ("opaque-updated",),
            )

        second = self.import_legacy_v2.execute(LegacyImportCommand(self.v2_database))

        self.assertNotEqual(first.source_database_sha256, second.source_database_sha256)
        with self.core.database.read() as connection:
            namespaces = {
                str(row[0])
                for row in connection.execute(
                    "SELECT source_namespace FROM legacy_import_runs WHERE source_path = ?",
                    (str(self.v2_database.resolve()),),
                )
            }
            self_people = int(
                connection.execute(
                    "SELECT COUNT(*) FROM persons WHERE kind = 'self'"
                ).fetchone()[0]
            )
        self.assertEqual(namespaces, {first_namespace})
        self.assertEqual(self_people, 1)

    def test_report_exposes_missing_conflicting_and_unmapped_legacy_data(self) -> None:
        result = self.import_legacy_v2.execute(LegacyImportCommand(self.v2_database))

        issue_codes = {str(issue["code"]) for issue in result.issues}
        self.assertIn("missing_file", issue_codes)
        self.assertIn("conflicting_path", issue_codes)
        self.assertIn("source_mismatch", issue_codes)
        self.assertIn("unmapped_table", issue_codes)
        self.assertEqual(result.unmapped["unknown_legacy_objects"], 1)

        namespace = result.source_database_sha256[:24]
        bad_session_ref = f"v2:{namespace}:recording_sessions:2"
        with self.core.database.read() as connection:
            row = connection.execute(
                "SELECT state, status_code, blocking_reason "
                "FROM recording_sessions WHERE legacy_ref = ?",
                (bad_session_ref,),
            ).fetchone()
        self.assertEqual(tuple(row), ("quarantined", "failed", "legacy_source_issue"))

    def test_automatic_postprocess_imports_generates_and_leaves_review_pending(
        self,
    ) -> None:
        with sqlite3.connect(self.v2_database) as connection:
            connection.execute(
                "UPDATE recording_sessions SET timezone = 'CST' WHERE id = 1"
            )
        reminders = _ReminderGenerator()
        insights = _InsightGenerator()

        def core_factory():
            return compose_v3_core(
                V3CorePaths.from_state_dir(self.directory / "v3-state"),
                reminder_generator=reminders,
                insight_generator=insights,
            )

        postprocess = build_v3_postprocessor(
            database_path=self.v2_database,
            v3_state_dir=self.directory / "v3-state",
            source_namespace="automatic-test",
            reasoning_effort="auto",
            core_factory=core_factory,
        )
        result = postprocess(
            UploadRecord(
                upload_id="a" * 32,
                relative_path="watch/session_summary.json",
                size=2,
                sha256="b" * 64,
                kind="manifest",
                status="completed",
                offset=2,
                created_at=STAMP,
                updated_at=STAMP,
            ),
            {"session_id": 1, "workflow_run_id": 7},
            lambda stage, detail: None,
        )

        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["automatic_approval"])
        self.assertEqual(result["pending_reminder_review_count"], 1)
        self.assertTrue(result["human_review_required"])
        self.assertEqual(len(reminders.requests), 1)
        self.assertEqual(len(insights.daily_requests), 1)
        self.assertEqual(insights.daily_requests[0].timezone, "Asia/Singapore")
        self.assertTrue(
            any(item["created_count"] > 0 for item in result["person_memories"])
        )

    def _core_counts(self) -> dict[str, int]:
        with self.core.database.read() as connection:
            return {
                table: int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in (
                    "recording_sessions",
                    "audio_assets",
                    "audio_replicas",
                    "capture_segments",
                    "processing_runs",
                    "artifacts",
                    "correction_operations",
                    "speaker_tracks",
                    "utterances",
                    "persons",
                    "generation_records",
                    "structured_change_proposals",
                    "event_operations",
                    "event_current_states",
                    "evidence_links",
                    "derivation_dependencies",
                    "change_events",
                )
            }


def _create_v2_fixture(database_path: Path) -> dict[str, list[object]]:
    good_payloads = [b"first-good-audio", b"second-good-audio"]
    conflict_payload = b"AAAA"
    declared_conflict_payload = b"BBBB"
    missing_payload = b"MISS"
    (database_path.parent / "good-1.m4a").write_bytes(good_payloads[0])
    (database_path.parent / "good-2.m4a").write_bytes(good_payloads[1])
    (database_path.parent / "conflict.m4a").write_bytes(conflict_payload)

    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY);
            INSERT INTO schema_migrations VALUES (15);

            CREATE TABLE recording_sessions (
                id INTEGER PRIMARY KEY,
                session_key TEXT NOT NULL,
                legacy_recording_id INTEGER,
                recorded_at TEXT NOT NULL,
                timezone TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE source_objects (
                id INTEGER PRIMARY KEY,
                sha256 TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                container TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE source_instances (
                id INTEGER PRIMARY KEY,
                instance_key TEXT NOT NULL,
                source_path TEXT NOT NULL,
                byte_size INTEGER NOT NULL,
                recorded_at TEXT NOT NULL,
                timezone TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE session_sources (
                id INTEGER PRIMARY KEY,
                session_id INTEGER NOT NULL,
                source_object_id INTEGER NOT NULL,
                source_instance_id INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                session_start_ms INTEGER NOT NULL,
                session_end_ms INTEGER NOT NULL,
                source_start_ms INTEGER NOT NULL,
                source_end_ms INTEGER NOT NULL,
                session_start_sample INTEGER
            );
            CREATE TABLE processing_runs (
                id INTEGER PRIMARY KEY,
                recording_id INTEGER,
                session_id INTEGER,
                run_kind TEXT NOT NULL,
                status TEXT NOT NULL,
                config_json TEXT,
                config_sha256 TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                error TEXT,
                pipeline_version TEXT
            );
            CREATE TABLE manual_identity_annotations (
                id INTEGER PRIMARY KEY,
                annotation_key TEXT NOT NULL UNIQUE,
                session_id INTEGER NOT NULL,
                diarization_run_id INTEGER NOT NULL,
                session_start_ms INTEGER NOT NULL,
                session_end_ms INTEGER NOT NULL,
                identity_label TEXT NOT NULL,
                anonymous_speaker_label TEXT NOT NULL,
                status TEXT NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE person_profiles (
                id INTEGER PRIMARY KEY,
                display_name TEXT NOT NULL,
                profile_type TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE asr_hypotheses (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL,
                session_id INTEGER NOT NULL,
                window_index INTEGER NOT NULL,
                hypothesis_role TEXT NOT NULL
            );
            CREATE TABLE asr_alignment_tokens (
                id INTEGER PRIMARY KEY,
                hypothesis_id INTEGER NOT NULL,
                token_index INTEGER NOT NULL,
                text TEXT NOT NULL,
                session_start_ms INTEGER NOT NULL,
                session_end_ms INTEGER NOT NULL,
                kept_in_core INTEGER NOT NULL
            );
            CREATE TABLE asr_token_sources (
                token_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                source_object_id INTEGER NOT NULL,
                source_sha256 TEXT NOT NULL,
                source_start_ms INTEGER NOT NULL,
                source_end_ms INTEGER NOT NULL,
                PRIMARY KEY(token_id, position)
            );
            CREATE TABLE token_speaker_attributions (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL,
                token_id INTEGER NOT NULL,
                speaker_label TEXT,
                attribution_kind TEXT NOT NULL,
                rank INTEGER NOT NULL,
                confidence REAL
            );
            CREATE TABLE semantic_exchanges (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL,
                session_id INTEGER NOT NULL,
                asr_run_id INTEGER NOT NULL,
                diarization_run_id INTEGER,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                request_format TEXT NOT NULL,
                response_format TEXT NOT NULL,
                request_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE semantic_candidates (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL,
                candidate_key TEXT NOT NULL,
                candidate_type TEXT NOT NULL,
                session_start_ms INTEGER NOT NULL,
                session_end_ms INTEGER NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                confidence REAL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE unknown_legacy_objects (
                id INTEGER PRIMARY KEY,
                payload TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO recording_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (1, "good-session", 101, STAMP, "Asia/Singapore", 3000, STAMP, STAMP),
                (2, "bad-session", 102, STAMP, "Asia/Singapore", 1500, STAMP, STAMP),
            ),
        )
        payloads = good_payloads + [
            conflict_payload,
            declared_conflict_payload,
            missing_payload,
        ]
        durations = [1000, 2000, 500, 500, 500]
        connection.executemany(
            "INSERT INTO source_objects VALUES (?, ?, ?, ?, ?)",
            tuple(
                (
                    index,
                    hashlib.sha256(payload).hexdigest(),
                    durations[index - 1],
                    "m4a",
                    STAMP,
                )
                for index, payload in enumerate(payloads, start=1)
            ),
        )
        instance_paths = [
            "good-1.m4a",
            "good-2.m4a",
            "conflict.m4a",
            "conflict.m4a",
            "missing.m4a",
        ]
        connection.executemany(
            "INSERT INTO source_instances VALUES (?, ?, ?, ?, ?, ?, ?)",
            tuple(
                (
                    index,
                    f"instance-{index}",
                    instance_paths[index - 1],
                    len(payloads[index - 1]),
                    STAMP,
                    "Asia/Singapore",
                    STAMP,
                )
                for index in range(1, 6)
            ),
        )
        connection.executemany(
            "INSERT INTO session_sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (1, 1, 1, 1, 0, 0, 1000, 0, 1000, 0),
                (2, 1, 2, 2, 1, 1000, 3000, 0, 2000, 16000),
                (3, 2, 3, 3, 0, 0, 500, 0, 500, 0),
                (4, 2, 4, 4, 1, 500, 1000, 0, 500, 8000),
                (5, 2, 5, 5, 2, 1000, 1500, 0, 500, 16000),
            ),
        )
        connection.execute(
            "INSERT INTO processing_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                None,
                1,
                "quality",
                "completed",
                "{}",
                hashlib.sha256(b"{}").hexdigest(),
                STAMP,
                STAMP,
                None,
                "v2-fixture",
            ),
        )
        connection.execute(
            "INSERT INTO processing_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                2,
                None,
                1,
                "semantic_v2e0",
                "completed",
                "{}",
                hashlib.sha256(b"semantic").hexdigest(),
                STAMP,
                STAMP,
                None,
                "v2-e.0.2",
            ),
        )
        connection.executemany(
            "INSERT INTO manual_identity_annotations VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    1,
                    "self-window",
                    1,
                    1,
                    0,
                    600,
                    "self",
                    "SPEAKER_00",
                    "active",
                    None,
                    STAMP,
                    STAMP,
                ),
                (
                    2,
                    "mother-window",
                    1,
                    1,
                    600,
                    1100,
                    "mother",
                    "SPEAKER_01",
                    "active",
                    None,
                    STAMP,
                    STAMP,
                ),
            ),
        )
        connection.execute(
            "INSERT INTO person_profiles VALUES (?, ?, ?, ?)",
            (2, "我", "self", STAMP),
        )
        connection.execute(
            "INSERT INTO asr_hypotheses VALUES (?, ?, ?, ?, ?)",
            (1, 1, 1, 0, "primary"),
        )
        connection.executemany(
            "INSERT INTO asr_alignment_tokens VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                (1, 1, 0, "我", 100, 300, 1),
                (2, 1, 1, "会", 300, 500, 1),
                (3, 1, 2, "去", 700, 900, 1),
                (4, 1, 3, "的", 900, 1000, 1),
            ),
        )
        connection.executemany(
            "INSERT INTO asr_token_sources VALUES (?, ?, ?, ?, ?, ?)",
            tuple(
                (
                    token_id,
                    0,
                    1,
                    hashlib.sha256(good_payloads[0]).hexdigest(),
                    start,
                    end,
                )
                for token_id, start, end in (
                    (1, 100, 300),
                    (2, 300, 500),
                    (3, 700, 900),
                    (4, 900, 1000),
                )
            ),
        )
        connection.executemany(
            "INSERT INTO token_speaker_attributions VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                (1, 1, 1, "SPEAKER_00", "primary", 0, 1.0),
                (2, 1, 2, "SPEAKER_00", "primary", 0, 1.0),
                (3, 1, 3, "SPEAKER_01", "primary", 0, 1.0),
                (4, 1, 4, "SPEAKER_01", "primary", 0, 1.0),
            ),
        )
        connection.execute(
            "INSERT INTO semantic_exchanges VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                2,
                1,
                1,
                1,
                "codex_manual_eval",
                "codex-session",
                "legacy-request-v1",
                "legacy-response-v1",
                hashlib.sha256(b"semantic-request").hexdigest(),
                STAMP,
            ),
        )
        connection.executemany(
            "INSERT INTO semantic_candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    1,
                    2,
                    "event-1",
                    "event",
                    50,
                    1050,
                    "一起出门",
                    "本人和母亲准备出门。",
                    0.9,
                    STAMP,
                ),
                (
                    2,
                    2,
                    "fact-1",
                    "fact",
                    50,
                    550,
                    "陈述 · self",
                    "本人表示会去。",
                    0.95,
                    STAMP,
                ),
                (
                    3,
                    2,
                    "summary-1",
                    "daily_summary",
                    50,
                    1050,
                    "旧总结",
                    "不能直接灌入 V3.6。",
                    0.9,
                    STAMP,
                ),
            ),
        )
        connection.execute(
            "INSERT INTO unknown_legacy_objects VALUES (?, ?)", (1, "opaque")
        )
        connection.commit()
    finally:
        connection.close()
    return {
        "good_durations": [1000, 2000],
        "good_sha256": [
            hashlib.sha256(payload).hexdigest() for payload in good_payloads
        ],
    }


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _ReminderGenerator:
    model_label = "codex-test"
    producer_version = "test"
    prompt_version = "test"
    extractor_version = "test"

    def __init__(self) -> None:
        self.requests = []

    def generate(self, request, effort):
        self.requests.append(request)
        utterance = request.utterances[0]
        return ReminderModelResult(
            intents=(
                {
                    "operation": "CREATE_TASK",
                    "title": "测试自动审核候选",
                    "actor_person_id": str(
                        utterance["speaker_reference_id"] or "unknown"
                    ),
                    "commitment_direction": "not_applicable",
                    "related_person_ids": [],
                    "scheduled_at": "2026-09-02T02:00:00Z",
                    "location": None,
                    "confidence": 0.9,
                    "evidence_utterance_ids": [utterance["utterance_id"]],
                    "needs_confirmation": True,
                    "target_event_id": None,
                    "expected_revision": 0,
                    "reason": "test",
                },
            ),
            turn_id="reminder-turn",
            reasoning_effort=effort,
        )

    def close(self):
        return None


class _InsightGenerator:
    model_label = "codex-test"
    producer_version = "test"
    prompt_version = "test"
    extractor_version = "test"

    def __init__(self) -> None:
        self.daily_requests = []

    def generate_daily(self, request, effort):
        self.daily_requests.append(request)
        return DailyInsightModelResult(
            narrative={section: () for section in DAILY_NARRATIVE_SECTIONS},
            turn_id="daily-turn",
            reasoning_effort=effort,
        )

    def generate_relationship(self, request, effort):
        return RelationshipInsightModelResult(
            observations=(),
            turn_id="relationship-turn",
            reasoning_effort=effort,
        )

    def close(self):
        return None


if __name__ == "__main__":
    unittest.main()
