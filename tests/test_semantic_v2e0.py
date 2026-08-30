from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from allday_asr.services.semantic_v2e0 import review_semantic_candidate
from allday_asr.services.semantic_v2e01 import (
    DeterministicMockSemanticProvider,
    SemanticV2E01Settings,
    build_provider_request,
    run_semantic_v2e01,
    semantic_overview,
)
from allday_asr.storage.database import Database


class SemanticV2E0Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parent
        self.token = uuid4().hex
        self.database_path = self.root / f"semantic-{self.token}.sqlite3"
        self.source_path = self.root / f"semantic-{self.token}.m4a"
        self.output_dir = self.root / f"semantic-output-{self.token}"
        self.source_path.write_bytes(b"immutable-semantic-source")
        self.source_sha256 = hashlib.sha256(self.source_path.read_bytes()).hexdigest()
        self.database = Database.open(self.database_path)
        recording = self.database.create_recording(
            {
                "source_path": str(self.source_path.resolve()),
                "sha256": self.source_sha256,
                "device": "watch",
                "recorded_at": "2026-08-29T08:00:00+08:00",
                "timezone": "Asia/Singapore",
                "duration_ms": 200_000,
                "codec": "aac",
                "sample_rate": 16_000,
                "channels": 1,
                "bit_rate": 64_000,
                "encoder": "test",
            }
        )
        self.recording_id = int(recording["id"])
        self.session = self.database.get_session_for_recording(self.recording_id)
        self.source = self.database.list_session_sources(int(self.session["id"]))[0]
        self.asr_run_id = self._create_asr_run()
        self.diarization_run_id = self._create_diarization_run()

    def tearDown(self) -> None:
        if self.output_dir.exists():
            shutil.rmtree(self.output_dir)
        self.source_path.unlink(missing_ok=True)
        for suffix in ("", "-shm", "-wal"):
            Path(f"{self.database_path}{suffix}").unlink(missing_ok=True)
        for backup in self.root.glob(f"semantic-{self.token}.schema-*.sqlite3"):
            backup.unlink(missing_ok=True)

    def test_local_exchange_is_immutable_private_and_reviewable(self) -> None:
        class CapturingProvider(DeterministicMockSemanticProvider):
            def __init__(self):
                self.request = None

            def generate(self, request):
                self.request = request
                return super().generate(request)

        provider = CapturingProvider()
        with patch(
            "allday_asr.services.semantic_v2e01.OUTPUT_DIR", self.output_dir
        ):
            summary = run_semantic_v2e01(
                self.database,
                self.recording_id,
                asr_run_id=self.asr_run_id,
                diarization_run_id=self.diarization_run_id,
                settings=SemanticV2E01Settings(),
                provider=provider,
            )

        self.assertEqual(summary.conversation_count, 1)
        self.assertEqual(summary.excluded_block_count, 0)
        self.assertEqual(summary.llm_job_count, 1)
        self.assertEqual(summary.token_count, 4)
        self.assertEqual(summary.candidate_count, 2)
        exchange = self.database.get_semantic_exchange(summary.run_id)
        self.assertFalse(exchange["audio_bytes_included"])
        self.assertFalse(exchange["source_paths_included"])
        request = json.loads(exchange["request_json"])
        response = json.loads(exchange["response_json"])
        self.assertFalse(request["privacy"]["network_access"])
        self.assertNotIn(str(self.source_path), exchange["request_json"])
        self.assertEqual(response["facts"], [])
        self.assertEqual(response["actions"], [])
        conversation = request["evidence_ledger"]["conversations"][0]
        self.assertGreater(conversation["duration_ms"], 120_000)
        self.assertEqual(len(conversation["review_clips"]), 2)
        self.assertTrue(
            all(
                token["source_refs"]
                for item in request["evidence_ledger"]["conversations"]
                for token in item["tokens"]
            )
        )
        provider_json = json.dumps(provider.request, ensure_ascii=False)
        self.assertNotIn("source_refs", provider_json)
        self.assertNotIn("source_sha256", provider_json)
        self.assertNotIn('"token_ids":', provider_json)
        self.assertIn("complete_conversation", provider_json)
        manifest = json.loads(summary.manifest_path.read_text(encoding="utf-8"))
        self.assertFalse(manifest["safety"]["network_access"])
        self.assertFalse(manifest["safety"]["audio_bytes_included"])
        self.assertFalse(manifest["safety"]["provider_receives_source_hashes"])

        overview = semantic_overview(self.database, self.recording_id)
        self.assertTrue(overview["available"])
        self.assertEqual(overview["run"]["asr_run_id"], self.asr_run_id)
        self.assertTrue(overview["transport"]["whole_conversation_first"])
        event = next(
            item for item in overview["candidates"] if item["type"] == "event"
        )
        self.assertEqual(event["semantic_unit"], "conversation")
        self.assertEqual(len(event["review_clips"]), 2)
        first_revision = review_semantic_candidate(
            self.database,
            self.recording_id,
            event["id"],
            status="confirmed",
            title="人工确认事件",
            body="人工修订内容",
        )
        self.assertEqual(first_revision["revision_index"], 1)
        second_revision = review_semantic_candidate(
            self.database,
            self.recording_id,
            event["id"],
            status="rejected",
            note="只是测试",
        )
        self.assertEqual(second_revision["revision_index"], 2)
        self.assertEqual(second_revision["title"], "人工确认事件")
        revisions = self.database.list_semantic_candidate_revisions(event["id"])
        self.assertEqual(len(revisions), 2)
        self.assertEqual(revisions[-1]["status"], "rejected")

        with (
            self.assertRaises(sqlite3.IntegrityError),
            self.database.connect() as connection,
        ):
            connection.execute(
                "UPDATE semantic_exchanges SET provider = 'changed' WHERE run_id = ?",
                (summary.run_id,),
            )
        self.assertEqual(
            hashlib.sha256(self.source_path.read_bytes()).hexdigest(),
            self.source_sha256,
        )

    def test_provider_cannot_replace_token_or_source_evidence(self) -> None:
        class BrokenEvidenceProvider(DeterministicMockSemanticProvider):
            provider_name = "broken-local-test"

            def generate(self, request):
                response = super().generate(request)
                response["conversations"][0]["evidence_utterance_keys"] = [
                    "invented-utterance"
                ]
                return response

        with (
            patch(
                "allday_asr.services.semantic_v2e01.OUTPUT_DIR",
                self.output_dir,
            ),
            self.assertRaisesRegex(ValueError, "证据引用被改变"),
        ):
            run_semantic_v2e01(
                self.database,
                self.recording_id,
                asr_run_id=self.asr_run_id,
                diarization_run_id=self.diarization_run_id,
                provider=BrokenEvidenceProvider(),
            )
        semantic_runs = [
            row
            for row in self.database.list_processing_runs(self.recording_id)
            if row["run_kind"] == "semantic_v2e0"
        ]
        self.assertEqual(semantic_runs[-1]["status"], "failed")
        with self.assertRaises(KeyError):
            self.database.get_semantic_exchange(int(semantic_runs[-1]["id"]))

    def test_provider_chunks_reassemble_as_one_complete_conversation(self) -> None:
        utterances = [
            {
                "key": f"utterance-{index:04d}",
                "start_ms": index * 1_000,
                "end_ms": index * 1_000 + 800,
                "speaker": "SPEAKER_00",
                "speaker_kind": "primary",
                "text": "这是一段需要保留完整上下文的测试对话" * 5,
                "uncertainty": {},
            }
            for index in range(60)
        ]
        conversation = {
            "key": "conversation-0001",
            "start_ms": 0,
            "end_ms": 59_800,
            "transcript": "".join(item["text"] for item in utterances),
            "speakers": ["SPEAKER_00"],
            "uncertainty": {},
            "utterances": utterances,
            "asr_alternatives": [],
        }
        request = build_provider_request(
            self.session,
            [conversation],
            settings=SemanticV2E01Settings(
                max_llm_request_chars=4_000,
                chunk_overlap_utterances=3,
            ),
        )
        self.assertGreater(len(request["jobs"]), 1)
        response = DeterministicMockSemanticProvider().generate(request)
        self.assertEqual(len(response["conversations"]), 1)
        output = response["conversations"][0]
        self.assertEqual(output["start_ms"], 0)
        self.assertEqual(output["end_ms"], 59_800)
        self.assertEqual(
            output["evidence_utterance_keys"],
            [item["key"] for item in utterances],
        )

    def _create_asr_run(self) -> int:
        run_id = self.database.start_processing_run(
            self.recording_id,
            run_kind="quality_asr_v2c",
            config={"test": True},
            config_sha256="semantic-asr",
            model_manifest={"model_id": "fake/qwen"},
            pipeline_version="v2-c-test",
        )
        source_common = {
            "source_object_id": int(self.source["source_object_id"]),
            "source_sha256": str(self.source["sha256"]),
        }
        token_values = [
            ("你", 1_000, 1_200),
            ("好", 1_200, 1_400),
            ("再", 150_000, 150_200),
            ("见", 150_200, 150_400),
        ]
        self.database.create_asr_hypothesis(
            {
                "hypothesis_key": f"semantic:{self.token}:primary",
                "run_id": run_id,
                "session_id": int(self.session["id"]),
                "window_index": 0,
                "hypothesis_role": "primary",
                "core_start_ms": 0,
                "core_end_ms": 200_000,
                "analysis_start_ms": 0,
                "analysis_end_ms": 200_000,
                "model_id": "fake/qwen",
                "backend": "fake",
                "language": "zh",
                "text": "你好再见",
            },
            [
                {
                    "token_index": index,
                    "text": text,
                    "session_start_ms": start_ms,
                    "session_end_ms": end_ms,
                    "analysis_start_ms": start_ms,
                    "analysis_end_ms": end_ms,
                    "kept_in_core": True,
                    "alignment_model_id": "fake/aligner",
                    "source_refs": [
                        {
                            **source_common,
                            "source_start_ms": start_ms,
                            "source_end_ms": end_ms,
                        }
                    ],
                }
                for index, (text, start_ms, end_ms) in enumerate(token_values)
            ],
        )
        self.database.finish_processing_run(
            run_id,
            status="completed",
            summary={"aligned_tokens": 4},
            artifacts={},
        )
        return run_id

    def _create_diarization_run(self) -> int:
        run_id = self.database.start_processing_run(
            self.recording_id,
            run_kind="quality_diarization_v2d",
            config={"test": True},
            config_sha256="semantic-diarization",
            model_manifest={"model_id": "fake/community"},
            pipeline_version="v2-d-test",
            parent_run_id=self.asr_run_id,
        )
        tokens = self.database.list_committed_asr_tokens(self.asr_run_id)
        self.database.create_token_speaker_attributions(
            run_id,
            self.asr_run_id,
            [
                {
                    "attribution_key": f"semantic:{self.token}:attr:{token['id']}",
                    "token_id": int(token["id"]),
                    "speaker_label": "SPEAKER_00" if index < 2 else None,
                    "attribution_kind": "primary" if index < 2 else "none",
                    "overlap_ms": int(token["session_end_ms"])
                    - int(token["session_start_ms"])
                    if index < 2
                    else 0,
                    "overlap_ratio": 1.0 if index < 2 else 0.0,
                    "rank": 0,
                    "confidence": 0.9 if index < 2 else None,
                }
                for index, token in enumerate(tokens)
            ],
        )
        self.database.finish_processing_run(
            run_id,
            status="completed",
            summary={"asr_run_id": self.asr_run_id},
            artifacts={},
        )
        return run_id


if __name__ == "__main__":
    unittest.main()
