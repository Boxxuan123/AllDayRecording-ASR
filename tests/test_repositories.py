from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from uuid import uuid4

from allday_asr.infrastructure.sqlite.repositories import (
    ActionRepository,
    AsrRepository,
    DiarizationRepository,
    EvaluationRepository,
    IdentityRepository,
    RunRepository,
    SemanticRepository,
    SessionRepository,
)
from allday_asr.storage.database import Database


class RepositoryFacadeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database_path = (
            Path(__file__).parent / f"repository-{uuid4().hex}.sqlite3"
        )
        self.database = Database.open(self.database_path)
        self.recording = self.database.create_recording(
            {
                "source_path": str(Path(__file__).parent / "repository-audio.m4a"),
                "sha256": uuid4().hex,
                "device": "test",
                "recorded_at": "2026-08-30T00:00:00+00:00",
                "timezone": "Asia/Singapore",
                "duration_ms": 10_000,
                "codec": "aac",
                "sample_rate": 16_000,
                "channels": 1,
                "bit_rate": 64_000,
                "encoder": "test",
            }
        )

    def tearDown(self) -> None:
        for candidate in (
            self.database_path,
            Path(f"{self.database_path}-shm"),
            Path(f"{self.database_path}-wal"),
        ):
            candidate.unlink(missing_ok=True)

    def test_session_repository_and_facade_share_behavior(self) -> None:
        self.assertIsInstance(self.database.sessions, SessionRepository)
        repository_row = self.database.sessions.get_session_for_recording(
            int(self.recording["id"])
        )
        facade_row = self.database.get_session_for_recording(
            int(self.recording["id"])
        )

        self.assertEqual(dict(repository_row), dict(facade_row))
        self.assertEqual(
            self.database.sessions.session_input_fingerprint(repository_row["id"]),
            self.database.session_input_fingerprint(repository_row["id"]),
        )
        self.assertEqual(
            [dict(row) for row in self.database.sessions.list_session_sources(
                repository_row["id"]
            )],
            [dict(row) for row in self.database.list_session_sources(
                repository_row["id"]
            )],
        )

    def test_run_repository_writes_are_visible_through_facade(self) -> None:
        self.assertIsInstance(self.database.runs, RunRepository)
        run_id = self.database.runs.start_processing_run(
            int(self.recording["id"]),
            run_kind="quality_asr_v2c",
            config={"source": "repository-test"},
            config_sha256="repository-config",
            model_manifest={"asr": "test"},
            pipeline_version="repository-v1",
        )

        self.assertEqual(self.database.get_processing_run(run_id)["status"], "running")
        self.assertEqual(len(self.database.list_processing_run_inputs(run_id)), 1)
        self.database.finish_processing_run(
            run_id,
            status="completed",
            summary={"result": "ok"},
        )

        self.assertEqual(self.database.runs.get_processing_run(run_id)["status"], "completed")
        self.assertEqual(
            [row["id"] for row in self.database.runs.list_processing_runs(
                int(self.recording["id"])
            )],
            [row["id"] for row in self.database.list_processing_runs(
                int(self.recording["id"])
            )],
        )

    def test_run_repository_rolls_back_run_when_input_insert_fails(self) -> None:
        session = self.database.sessions.get_session_for_recording(
            int(self.recording["id"])
        )
        original_list_sources = self.database.sessions.list_session_sources
        self.database.sessions.list_session_sources = lambda _session_id: [
            {
                "source_object_id": 999_999,
                "source_instance_id": 999_999,
                "instance_key": "missing-instance",
                "sha256": "missing-source",
                "session_start_ms": 0,
                "session_end_ms": 100,
                "source_start_ms": 0,
                "source_end_ms": 100,
                "session_start_sample": None,
                "session_end_sample": None,
                "source_start_sample": None,
                "source_end_sample": None,
                "timeline_sample_rate": None,
            }
        ]
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                self.database.runs.start_processing_run(
                    int(self.recording["id"]),
                    run_kind="quality_asr_v2c",
                    config={},
                    config_sha256="invalid-input",
                )
        finally:
            self.database.sessions.list_session_sources = original_list_sources

        self.assertEqual(
            self.database.runs.list_session_processing_runs(int(session["id"])), []
        )

    def test_semantic_repository_and_facade_share_one_transaction_boundary(
        self,
    ) -> None:
        self.assertIsInstance(self.database.semantic, SemanticRepository)
        asr_run_id = self.database.runs.start_processing_run(
            int(self.recording["id"]),
            run_kind="quality_asr_v2c",
            config={},
            config_sha256="asr-config",
        )
        self.database.runs.finish_processing_run(asr_run_id, status="completed")
        semantic_run_id = self.database.start_processing_run(
            int(self.recording["id"]),
            run_kind="semantic_v2e0",
            config={},
            config_sha256="semantic-config",
        )

        rows = self.database.semantic.create_semantic_snapshot(
            semantic_run_id,
            {
                "asr_run_id": asr_run_id,
                "diarization_run_id": None,
                "provider": "local_test",
                "model": "deterministic",
                "request_format": "test.request.v1",
                "response_format": "test.response.v1",
                "request": {"input": "test"},
                "response": {"output": "test"},
                "audio_bytes_included": False,
                "source_paths_included": False,
            },
            [
                {
                    "candidate_key": "repository-candidate-1",
                    "candidate_type": "event",
                    "session_start_ms": 100,
                    "session_end_ms": 500,
                    "title": "测试事件",
                    "body": "用于验证 repository 与兼容门面的行为一致。",
                    "confidence": 0.9,
                    "evidence": {"kind": "test"},
                }
            ],
        )

        self.assertEqual(
            [dict(row) for row in rows],
            [dict(row) for row in self.database.list_semantic_candidates(
                semantic_run_id
            )],
        )
        self.database.finish_processing_run(semantic_run_id, status="completed")
        revision = self.database.semantic.create_semantic_candidate_revision(
            int(rows[0]["id"]),
            status="confirmed",
            note="repository direct write",
        )
        self.assertEqual(revision["revision_index"], 1)
        self.assertEqual(
            [dict(revision)],
            [dict(row) for row in self.database.list_semantic_candidate_revisions(
                int(rows[0]["id"])
            )],
        )

    def test_semantic_repository_rolls_back_exchange_and_candidates_together(
        self,
    ) -> None:
        asr_run_id = self.database.runs.start_processing_run(
            int(self.recording["id"]),
            run_kind="quality_asr_v2c",
            config={},
            config_sha256="rollback-asr",
        )
        self.database.runs.finish_processing_run(asr_run_id, status="completed")
        semantic_run_id = self.database.runs.start_processing_run(
            int(self.recording["id"]),
            run_kind="semantic_v2e0",
            config={},
            config_sha256="rollback-semantic",
        )
        exchange = {
            "asr_run_id": asr_run_id,
            "diarization_run_id": None,
            "provider": "local_test",
            "model": "deterministic",
            "request_format": "test.request.v1",
            "response_format": "test.response.v1",
            "request": {},
            "response": {},
            "audio_bytes_included": False,
            "source_paths_included": False,
        }
        duplicate_candidates = [
            {
                "candidate_key": "duplicate-key",
                "candidate_type": "event",
                "session_start_ms": start_ms,
                "session_end_ms": start_ms + 100,
                "title": "事务测试",
                "body": "候选键重复时，exchange 和全部 candidate 必须一起回滚。",
                "confidence": 0.8,
                "evidence": {},
            }
            for start_ms in (100, 300)
        ]

        with self.assertRaises(sqlite3.IntegrityError):
            self.database.semantic.create_semantic_snapshot(
                semantic_run_id, exchange, duplicate_candidates
            )

        with self.assertRaises(KeyError):
            self.database.semantic.get_semantic_exchange(semantic_run_id)
        self.assertEqual(
            self.database.semantic.list_semantic_candidates(semantic_run_id), []
        )

    def test_second_batch_repositories_share_facade_behavior(self) -> None:
        self.assertIsInstance(self.database.asr, AsrRepository)
        self.assertIsInstance(self.database.diarization, DiarizationRepository)
        self.assertIsInstance(self.database.identity, IdentityRepository)
        self.assertIsInstance(self.database.evaluation, EvaluationRepository)
        self.assertIsInstance(self.database.actions, ActionRepository)

        recording_id = int(self.recording["id"])
        self.database.asr.replace_vad_segments(
            recording_id,
            [(100, 400)],
            str(self.recording["source_path"]),
        )
        segment = self.database.all_segments(recording_id)[0]
        self.database.diarization.replace_speaker_labels(
            recording_id,
            [(int(segment["id"]), "speaker-1")],
        )
        self.assertEqual(
            self.database.identity.get_segment(int(segment["id"]))[
                "speaker_session_id"
            ],
            "speaker-1",
        )

        evaluation_id = self.database.evaluation.record_evaluation_run(
            recording_id,
            truth_path="truth.json",
            truth_sha256="truth-sha",
            config={"source": "repository-test"},
            metrics={"wer": 0.0},
            report_json_path="report.json",
            report_markdown_path="report.md",
        )
        self.assertEqual(
            self.database.list_evaluation_runs(recording_id)[0]["id"],
            evaluation_id,
        )

        candidate = self.database.actions.upsert_action_candidate(
            {
                "recording_id": recording_id,
                "candidate_key": "repository-action-1",
                "candidate_type": "todo",
                "start_ms": 100,
                "end_ms": 400,
                "source_segment_ids": [int(segment["id"])],
                "title": "验证第二批仓储",
                "confidence": 0.9,
                "evidence": {"source": "repository-test"},
            }
        )
        self.assertEqual(
            dict(self.database.get_action_candidate(int(candidate["id"]))),
            dict(candidate),
        )


if __name__ == "__main__":
    unittest.main()
