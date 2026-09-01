from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import unittest
from pathlib import Path
from uuid import uuid4

from typer.testing import CliRunner

from allday_asr.cli import app
from allday_asr.v3.adapters.legacy_v2.label_migration import (
    LEGACY_LABEL_MIGRATION_FORMAT,
    migrate_legacy_labels,
)


class V31LegacyLabelMigrationTests(unittest.TestCase):
    def test_labels_are_normalized_deduplicated_and_source_is_read_only(self) -> None:
        directory = Path(__file__).parent / f"v31-label-migration-{uuid4().hex}"
        directory.mkdir()
        source = directory / "legacy.sqlite3"
        output = directory / "labels"
        try:
            _create_legacy_database(source)
            before = _sha256(source)

            summary = migrate_legacy_labels(source, output_dir=output)
            repeated = migrate_legacy_labels(source, output_dir=output)

            self.assertEqual(_sha256(source), before)
            self.assertEqual(summary.bundle_sha256, repeated.bundle_sha256)
            self.assertEqual(summary.receipt_sha256, repeated.receipt_sha256)
            bundle = json.loads(summary.bundle_path.read_text(encoding="utf-8"))
            self.assertEqual(bundle["format"], LEGACY_LABEL_MIGRATION_FORMAT)
            self.assertEqual(bundle["source"]["read_only"], True)
            self.assertEqual(bundle["counts"]["truth_sets"], 1)
            self.assertEqual(len(bundle["truth_annotations"]), 3)
            self.assertEqual(bundle["counts"]["timeline_reference_candidates"], 1)
            self.assertEqual(bundle["assessment"]["seam_quality"]["seams_with_legacy_prefill"], 1)
            self.assertEqual(bundle["assessment"]["identity"]["effective_self_windows"], 3)
            self.assertEqual(bundle["assessment"]["identity"]["effective_not_self_windows"], 1)
            self.assertEqual(bundle["assessment"]["identity"]["media_records"], 1)
            self.assertEqual(
                bundle["assessment"]["identity"]
                ["already_scored_eligible_self_holdout_samples"],
                0,
            )
            self.assertTrue(summary.report_path.is_file())
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_cli_writes_machine_readable_summary(self) -> None:
        directory = Path(__file__).parent / f"v31-label-cli-{uuid4().hex}"
        directory.mkdir()
        source = directory / "legacy.sqlite3"
        try:
            _create_legacy_database(source)
            result = CliRunner().invoke(
                app,
                [
                    "label-migrate",
                    str(source),
                    "--output-dir",
                    str(directory / "output"),
                ],
                env={"ALLDAY_V3_ENABLED": "1"},
            )
            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["counts"]["truth_sets"], 1)
            self.assertTrue(Path(payload["bundle"]).is_file())
            self.assertTrue(Path(payload["receipt"]).is_file())
            self.assertTrue(Path(payload["report"]).is_file())
        finally:
            shutil.rmtree(directory, ignore_errors=True)


def _create_legacy_database(path: Path) -> None:
    connection = sqlite3.connect(path)
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
                status TEXT NOT NULL
            );
            INSERT INTO recording_sessions VALUES (
                1, 'legacy-recording:1', 1, '2026-01-01T00:00:00Z',
                'Asia/Singapore', 900000, 'closed'
            );

            CREATE TABLE source_objects (
                id INTEGER PRIMARY KEY,
                sha256 TEXT NOT NULL
            );
            INSERT INTO source_objects VALUES (
                1, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
            );
            CREATE TABLE session_sources (
                id INTEGER PRIMARY KEY,
                session_id INTEGER NOT NULL,
                source_object_id INTEGER NOT NULL,
                source_instance_id INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                session_start_ms INTEGER NOT NULL,
                session_end_ms INTEGER NOT NULL,
                source_start_ms INTEGER NOT NULL
            );
            INSERT INTO session_sources VALUES (1, 1, 1, 1, 0, 0, 900000, 0);

            CREATE TABLE truth_sets (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                session_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                scope_start_ms INTEGER NOT NULL,
                scope_end_ms INTEGER NOT NULL,
                input_fingerprint TEXT NOT NULL,
                truth_sha256 TEXT NOT NULL,
                completeness_json TEXT NOT NULL
            );
            INSERT INTO truth_sets VALUES (
                1, 'old-truth', 1, 'frozen', 0, 900000, 'fingerprint',
                'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                '{"alignment":"none","transcript":"sparse","speaker":"sparse","overlap":"none","identity":"sparse"}'
            );
            CREATE TABLE truth_annotations (
                id INTEGER PRIMARY KEY,
                truth_set_id INTEGER NOT NULL,
                annotation_key TEXT NOT NULL,
                annotation_kind TEXT NOT NULL,
                session_start_ms INTEGER NOT NULL,
                session_end_ms INTEGER NOT NULL,
                label TEXT,
                text TEXT
            );
            INSERT INTO truth_annotations VALUES
                (1, 1, 'text:1', 'transcript', 297000, 299500, NULL, '你好'),
                (2, 1, 'speaker:1', 'speaker', 297000, 299500, 'self', NULL),
                (3, 1, 'identity:1', 'identity', 297000, 299500, 'self', NULL);
            CREATE TABLE truth_annotation_sources (
                annotation_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                source_object_id INTEGER NOT NULL,
                source_instance_id INTEGER,
                source_sha256 TEXT NOT NULL,
                source_start_ms INTEGER NOT NULL,
                source_end_ms INTEGER NOT NULL
            );
            INSERT INTO truth_annotation_sources VALUES
                (1, 0, 1, 1,
                 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                 297000, 299500),
                (3, 0, 1, 1,
                 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                 297000, 299500);

            CREATE TABLE manual_identity_annotations (
                id INTEGER PRIMARY KEY,
                session_id INTEGER NOT NULL,
                session_start_ms INTEGER NOT NULL,
                session_end_ms INTEGER NOT NULL,
                identity_label TEXT NOT NULL,
                anonymous_speaker_label TEXT NOT NULL,
                status TEXT NOT NULL
            );
            INSERT INTO manual_identity_annotations VALUES
                (1, 1, 10000, 15000, 'self', 'SPEAKER_00', 'active'),
                (2, 1, 10000, 15000, 'self', 'SPEAKER_00', 'active'),
                (3, 1, 20000, 26000, 'self', 'SPEAKER_00', 'active'),
                (4, 1, 30000, 35000, 'mother', 'SPEAKER_01', 'active'),
                (5, 1, 40000, 45000, 'tv', 'SPEAKER_02', 'active');

            CREATE TABLE voice_library_samples (
                id INTEGER PRIMARY KEY,
                sample_key TEXT NOT NULL,
                identity_label TEXT NOT NULL,
                split TEXT NOT NULL,
                session_key TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                speech_ms INTEGER NOT NULL,
                embedding_blob BLOB,
                embedding_model TEXT,
                embedding_version TEXT,
                source_path TEXT,
                stored_path TEXT,
                source_sha256 TEXT,
                recording_id INTEGER,
                segment_id INTEGER
            );
            INSERT INTO voice_library_samples VALUES (
                1, 'long-enrollment', '我', 'accepted', 'enrollment:one',
                120000, 100000, NULL, 'cam++', '1', 'enrollment.wav',
                NULL, NULL, NULL, NULL
            );
            """
        )
        connection.commit()
    finally:
        connection.close()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
