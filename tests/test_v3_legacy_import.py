from __future__ import annotations

import hashlib
import shutil
import sqlite3
import unittest
from pathlib import Path
from uuid import uuid4

from allday_asr.v3.application import LegacyImportCommand
from allday_asr.v3.bootstrap import V3CorePaths, compose_v3_core


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

    def tearDown(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_repeated_read_only_import_is_idempotent_and_preserves_session_graph(
        self,
    ) -> None:
        source_before = _digest(self.v2_database)

        first = self.core.import_legacy_v2.execute(
            LegacyImportCommand(self.v2_database)
        )
        counts_after_first = self._core_counts()
        second = self.core.import_legacy_v2.execute(
            LegacyImportCommand(self.v2_database)
        )
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

        namespace = source_before[:24]
        session_ref = f"v2:{namespace}:recording_sessions:1"
        with self.core.database.read() as connection:
            session = connection.execute(
                "SELECT * FROM recording_sessions WHERE legacy_ref = ?",
                (session_ref,),
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

    def test_report_exposes_missing_conflicting_and_unmapped_legacy_data(self) -> None:
        result = self.core.import_legacy_v2.execute(
            LegacyImportCommand(self.v2_database)
        )

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
        self.assertEqual(
            tuple(row), ("quarantined", "failed", "legacy_source_issue")
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
                segment_id INTEGER NOT NULL,
                person_id INTEGER NOT NULL,
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
            "INSERT INTO manual_identity_annotations VALUES (?, ?, ?, ?)",
            (1, 7, 9, STAMP),
        )
        connection.execute(
            "INSERT INTO unknown_legacy_objects VALUES (?, ?)", (1, "opaque")
        )
        connection.commit()
    finally:
        connection.close()
    return {
        "good_durations": [1000, 2000],
        "good_sha256": [hashlib.sha256(payload).hexdigest() for payload in good_payloads],
    }


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
