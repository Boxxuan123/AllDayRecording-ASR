from __future__ import annotations

import hashlib
import sqlite3
import unittest
from pathlib import Path
from uuid import uuid4

from allday_asr.storage.database import (
    LATEST_SCHEMA_VERSION,
    Database,
    MIGRATIONS,
    SCHEMA,
)


class DatabaseTests(unittest.TestCase):
    def test_recording_dedup_and_resumable_segments(self) -> None:
        database_path = Path(__file__).parent / f"test-{uuid4().hex}.sqlite3"
        try:
            database = Database(database_path)
            self.assertEqual(database.schema_version(), LATEST_SCHEMA_VERSION)
            values = {
                "source_path": str(Path(__file__).parent / "audio.m4a"),
                "sha256": "abc123",
                "device": "test",
                "recorded_at": "2026-08-24T12:00:00+00:00",
                "timezone": "Asia/Singapore",
                "duration_ms": 10_000,
                "codec": "aac",
                "sample_rate": 16_000,
                "channels": 1,
                "bit_rate": 64_000,
                "encoder": "test",
            }
            recording = database.create_recording(values)
            self.assertEqual(recording["id"], 1)
            self.assertEqual(database.find_recording_by_hash("abc123")["id"], 1)
            source = database.list_source_objects()[0]
            session = database.get_session_for_recording(1)
            session_sources = database.list_session_sources(session["id"])
            self.assertEqual(source["sha256"], "abc123")
            self.assertEqual(source["storage_class"], "original_permanent")
            self.assertEqual(session["session_key"], "legacy-recording:1")
            self.assertEqual(len(session_sources), 1)
            self.assertEqual(session_sources[0]["session_end_ms"], 10_000)
            with self.assertRaises(sqlite3.IntegrityError):
                with database.connect() as connection:
                    connection.execute(
                        "UPDATE source_objects SET sha256 = 'changed' WHERE id = ?",
                        (source["id"],),
                    )
            with self.assertRaises(sqlite3.IntegrityError):
                with database.connect() as connection:
                    connection.execute(
                        "DELETE FROM source_objects WHERE id = ?", (source["id"],)
                    )

            database.replace_vad_segments(1, [(100, 900), (1_200, 2_000)], values["source_path"])
            self.assertEqual(database.segment_count(1), 2)
            first = database.pending_segments(1, limit=1)[0]
            database.mark_segment_running(first["id"])
            self.assertEqual(database.reset_interrupted_segments(1), 1)
            first = database.pending_segments(1, limit=1)[0]
            database.mark_segment_running(first["id"])
            database.mark_segment_completed(
                first["id"],
                language="zh",
                text_raw="测试",
                text_display="测试",
                asr_model="test-model",
            )
            self.assertEqual(database.segment_status_counts(1), {"completed": 1, "pending": 1})

            profile = database.upsert_self_profile(
                display_name="我",
                embedding_model="test-speaker-model",
                embedding_version="1",
                embedding_path="voiceprint.npz",
            )
            database.replace_speaker_labels(1, [(first["id"], "speaker_00")])
            self.assertEqual(
                database.assign_person_to_speaker(1, "speaker_00", profile["id"]), 1
            )
            self.assertEqual(database.person_assignment_count(profile["id"]), 1)
            self.assertEqual(database.clear_person_assignments(1, profile["id"]), 1)
            self.assertEqual(database.person_assignment_count(profile["id"]), 0)
            run_id = database.start_processing_run(
                1,
                run_kind="daily",
                config={"version": 1},
                config_sha256="config-hash",
                model_manifest={"asr": "test-model"},
                pipeline_version="v2-test",
                code_version="test-sha",
            )
            database.finish_processing_run(
                run_id,
                status="completed",
                summary={"segments": 2},
                artifacts={"timeline": "timeline.md"},
            )
            run = database.list_processing_runs(1)[0]
            self.assertEqual(run["status"], "completed")
            self.assertEqual(run["config_sha256"], "config-hash")
            self.assertTrue(run["input_fingerprint"])
            self.assertEqual(run["session_id"], session["id"])
            self.assertEqual(run["pipeline_version"], "v2-test")
            self.assertEqual(len(database.list_processing_run_inputs(run_id)), 1)
            sample = database.upsert_voice_library_sample(
                {
                    "sample_key": "test:sample:1",
                    "person_id": profile["id"],
                    "identity_label": "我",
                    "sample_type": "confirmed_conversation",
                    "split": "holdout",
                    "source_path": values["source_path"],
                    "recording_id": 1,
                    "segment_id": first["id"],
                    "session_key": "recording:1",
                    "duration_ms": 800,
                    "speech_ms": 800,
                    "human_confirmed": True,
                }
            )
            self.assertEqual(sample["sample_key"], "test:sample:1")
            self.assertEqual(len(database.list_voice_library_samples(person_id=profile["id"])), 1)
            self.assertTrue(database.delete_person_profile(profile["id"]))
            self.assertIsNone(database.get_self_profile())
        finally:
            for suffix in ("", "-shm", "-wal"):
                candidate = Path(f"{database_path}{suffix}")
                if candidate.exists():
                    candidate.unlink()

    def test_schema_v3_is_backed_up_and_backfilled(self) -> None:
        root = Path(__file__).parent
        token = uuid4().hex
        database_path = root / f"migration-{token}.sqlite3"
        source_path = root / f"migration-{token}.m4a"
        source_bytes = b"immutable-watch-audio"
        source_path.write_bytes(source_bytes)
        digest = hashlib.sha256(source_bytes).hexdigest()
        connection = sqlite3.connect(database_path)
        try:
            connection.executescript(SCHEMA)
            connection.execute(
                """
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO schema_migrations VALUES (1, '2026-08-28T00:00:00+00:00')"
            )
            for version in (2, 3):
                connection.executescript(MIGRATIONS[version])
                connection.execute(
                    "INSERT INTO schema_migrations VALUES (?, '2026-08-28T00:00:00+00:00')",
                    (version,),
                )
            connection.execute(
                """
                INSERT INTO recordings (
                    source_path, sha256, device, recorded_at, timezone,
                    duration_ms, codec, sample_rate, channels, bit_rate,
                    encoder, status, created_at, updated_at
                ) VALUES (?, ?, 'watch', '2026-08-28T00:00:00+00:00',
                          'Asia/Singapore', 5000, 'aac', 16000, 1, 64000,
                          'test', 'imported', '2026-08-28T00:00:00+00:00',
                          '2026-08-28T00:00:00+00:00')
                """,
                (str(source_path.resolve()), digest),
            )
            connection.commit()
        finally:
            connection.close()

        backup_paths: list[Path] = []
        try:
            database = Database(database_path)
            self.assertEqual(database.schema_version(), LATEST_SCHEMA_VERSION)
            source = database.list_source_objects()[0]
            session = database.get_session_for_recording(1)
            self.assertEqual(source["sha256"], digest)
            self.assertEqual(source["source_path"], str(source_path.resolve()))
            self.assertEqual(session["duration_ms"], 5000)
            backup_paths = list(
                root.glob(
                    f"migration-{token}.schema-v3-to-v{LATEST_SCHEMA_VERSION}.*.sqlite3"
                )
            )
            self.assertEqual(len(backup_paths), 1)
            backup = sqlite3.connect(backup_paths[0])
            try:
                version = backup.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                self.assertEqual(version, 3)
            finally:
                backup.close()
        finally:
            source_path.unlink(missing_ok=True)
            for candidate in [
                database_path,
                Path(f"{database_path}-shm"),
                Path(f"{database_path}-wal"),
                *backup_paths,
            ]:
                candidate.unlink(missing_ok=True)

    def test_schema_v4_is_backed_up_before_v2_b_migration(self) -> None:
        root = Path(__file__).parent
        token = uuid4().hex
        database_path = root / f"v2b-migration-{token}.sqlite3"
        connection = sqlite3.connect(database_path)
        try:
            connection.executescript(SCHEMA)
            connection.execute(
                """
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO schema_migrations VALUES (1, '2026-08-28T00:00:00+00:00')"
            )
            for version in (2, 3, 4):
                connection.executescript(MIGRATIONS[version])
                connection.execute(
                    "INSERT INTO schema_migrations VALUES (?, '2026-08-28T00:00:00+00:00')",
                    (version,),
                )
            connection.commit()
        finally:
            connection.close()

        backup_pattern = f"v2b-migration-{token}.schema-v4-to-v5.*.sqlite3"
        try:
            database = Database(database_path)
            self.assertEqual(database.schema_version(), 5)
            with database.connect() as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertIn("truth_sets", tables)
            self.assertIn("benchmark_prediction_sets", tables)
            backups = list(root.glob(backup_pattern))
            self.assertEqual(len(backups), 1)
            backup = sqlite3.connect(backups[0])
            try:
                self.assertEqual(
                    backup.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()[0],
                    4,
                )
            finally:
                backup.close()
        finally:
            for candidate in [
                database_path,
                Path(f"{database_path}-shm"),
                Path(f"{database_path}-wal"),
                *root.glob(backup_pattern),
            ]:
                candidate.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
