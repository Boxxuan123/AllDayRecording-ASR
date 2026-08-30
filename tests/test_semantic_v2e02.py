from __future__ import annotations

import hashlib
import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from allday_asr.services.semantic_v2e0 import review_semantic_candidate
from allday_asr.services.semantic_v2e02 import (
    EVIDENCE_LEDGER_FORMAT,
    SEMANTIC_RESPONSE_FORMAT,
    EpisodeContractMockProvider,
    ReplaySemanticProvider,
    SemanticV2E02Settings,
    assemble_transport_episodes,
    build_provider_request,
    prepare_semantic_v2e02,
    run_semantic_v2e02,
    semantic_overview,
    validate_semantic_response,
)
from allday_asr.storage.database import Database


class SemanticV2E02Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parent
        self.token = uuid4().hex
        self.database_path = self.root / f"semantic-e02-{self.token}.sqlite3"
        self.source_path = self.root / f"semantic-e02-{self.token}.m4a"
        self.truth_path = self.root / f"semantic-e02-truth-{self.token}.json"
        self.output_dir = self.root / f"semantic-e02-output-{self.token}"
        self.source_path.write_bytes(b"immutable-semantic-e02-source")
        self.truth_path.write_text("{}\n", encoding="utf-8")
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
        self._create_truth_set()
        self._create_contamination_audit()

    def tearDown(self) -> None:
        if self.output_dir.exists():
            shutil.rmtree(self.output_dir)
        self.source_path.unlink(missing_ok=True)
        self.truth_path.unlink(missing_ok=True)
        for suffix in ("", "-shm", "-wal"):
            Path(f"{self.database_path}{suffix}").unlink(missing_ok=True)
        for backup in self.root.glob(f"semantic-e02-{self.token}.schema-*.sqlite3"):
            backup.unlink(missing_ok=True)

    def test_episode_payload_separates_four_evidence_tracks(self) -> None:
        class CapturingProvider(EpisodeContractMockProvider):
            def __init__(self) -> None:
                self.request = None

            def generate(self, request):
                self.request = request
                return super().generate(request)

        provider = CapturingProvider()
        with patch(
            "allday_asr.application.semantic.current.OUTPUT_DIR", self.output_dir
        ):
            summary = run_semantic_v2e02(
                self.database,
                self.recording_id,
                asr_run_id=self.asr_run_id,
                diarization_run_id=self.diarization_run_id,
                provider=provider,
            )

        self.assertEqual(summary.episode_count, 1)
        self.assertEqual(summary.llm_job_count, 1)
        self.assertEqual(summary.candidate_count, 1)
        self.assertEqual(summary.scene_count, 0)
        exchange = self.database.get_semantic_exchange(summary.run_id)
        self.assertEqual(exchange["request_format"], EVIDENCE_LEDGER_FORMAT)
        request = json.loads(exchange["request_json"])
        episode = request["evidence_ledger"]["episodes"][0]
        self.assertGreater(episode["duration_ms"], 120_000)
        self.assertEqual(len(episode["review_clips"]), 2)
        self.assertFalse(episode["boundary"]["is_semantic_scene"])
        self.assertTrue(episode["voice_clusters"][0]["contaminated"])

        first, second = episode["utterances"]
        self.assertEqual(first["source"]["label"], "live_person")
        self.assertEqual(first["identity"]["label"], "mother")
        self.assertFalse(first["voice_cluster"]["identity_safe"])
        self.assertEqual(second["source"]["label"], "media_playback")
        self.assertEqual(second["identity"]["label"], "unknown")

        provider_json = json.dumps(provider.request, ensure_ascii=False)
        self.assertNotIn('"transcript":', provider_json)
        self.assertNotIn("source_refs", provider_json)
        self.assertNotIn("source_sha256", provider_json)
        self.assertNotIn('"token_ids":', provider_json)
        self.assertIn('"utterances":', provider_json)
        self.assertEqual(
            hashlib.sha256(self.source_path.read_bytes()).hexdigest(),
            self.source_sha256,
        )

        overview = semantic_overview(self.database, self.recording_id)
        self.assertEqual(overview["version"], "v2-e.0.2")
        self.assertTrue(overview["transport"]["episode_is_context_container"])
        candidate = overview["candidates"][0]
        revision = review_semantic_candidate(
            self.database,
            self.recording_id,
            candidate["id"],
            status="confirmed",
            title="人工确认摘要",
            body="保留证据，不回写 ASR。",
        )
        self.assertEqual(revision["revision_index"], 1)

    def test_manual_response_creates_grounded_candidates(self) -> None:
        prepared = prepare_semantic_v2e02(
            self.database,
            self.recording_id,
            asr_run_id=self.asr_run_id,
            diarization_run_id=self.diarization_run_id,
        )
        episode = prepared.episodes[0]
        first = episode["utterances"][0]
        second = episode["utterances"][1]
        response = self._manual_response(
            prepared.provider_request,
            episode_key=str(episode["key"]),
            start_ms=int(episode["start_ms"]),
            end_ms=int(episode["end_ms"]),
            first_key=str(first["key"]),
            second_key=str(second["key"]),
        )
        with patch(
            "allday_asr.application.semantic.current.OUTPUT_DIR", self.output_dir
        ):
            summary = run_semantic_v2e02(
                self.database,
                self.recording_id,
                asr_run_id=self.asr_run_id,
                diarization_run_id=self.diarization_run_id,
                provider=ReplaySemanticProvider(response),
            )

        self.assertEqual(summary.scene_count, 1)
        self.assertEqual(summary.claim_count, 1)
        self.assertEqual(summary.action_count, 1)
        self.assertEqual(summary.candidate_count, 4)
        overview = semantic_overview(self.database, self.recording_id)
        self.assertTrue(overview["run"]["manual_eval"])
        units = {item["semantic_unit"] for item in overview["candidates"]}
        self.assertEqual(units, {"day", "scene", "claim", "action"})
        action = next(item for item in overview["candidates"] if item["type"] == "action")
        self.assertTrue(action["evidence"]["requires_human_confirmation"])

    def test_response_rejects_unsupported_identity_and_media_fact(self) -> None:
        prepared = prepare_semantic_v2e02(
            self.database,
            self.recording_id,
            asr_run_id=self.asr_run_id,
            diarization_run_id=self.diarization_run_id,
        )
        episode = prepared.episodes[0]
        first, second = episode["utterances"]
        response = self._manual_response(
            prepared.provider_request,
            episode_key=str(episode["key"]),
            start_ms=int(episode["start_ms"]),
            end_ms=int(episode["end_ms"]),
            first_key=str(first["key"]),
            second_key=str(second["key"]),
        )
        response["claims"][0]["subject"] = "father"
        with self.assertRaisesRegex(ValueError, "主体没有区间身份依据"):
            validate_semantic_response(
                response,
                prepared.episodes,
                provider_request_sha256=self._request_sha(prepared.provider_request),
            )

        response = self._manual_response(
            prepared.provider_request,
            episode_key=str(episode["key"]),
            start_ms=int(episode["start_ms"]),
            end_ms=int(episode["end_ms"]),
            first_key=str(first["key"]),
            second_key=str(second["key"]),
        )
        response["claims"][0].update(
            {
                "subject": "unknown",
                "claim_type": "personal_fact",
                "evidence_utterance_ids": [str(second["key"])],
            }
        )
        with self.assertRaisesRegex(ValueError, "纯媒体证据"):
            validate_semantic_response(
                response,
                prepared.episodes,
                provider_request_sha256=self._request_sha(prepared.provider_request),
            )

    def test_provider_chunks_reassemble_one_episode_without_semantic_split(self) -> None:
        utterances = [
            {
                "key": f"episode-0001:u-{index:04d}",
                "start_ms": index * 1_000,
                "end_ms": index * 1_000 + 800,
                "text": "这是一段需要保留完整上下文的测试对话" * 5,
                "voice_cluster": {
                    "label": "SPEAKER_00",
                    "assignment": "primary",
                    "identity_safe": False,
                    "contaminated": False,
                },
                "source": {"label": "unknown", "resolution": "none", "evidence": []},
                "identity": {"label": "unknown", "resolution": "none", "evidence": []},
                "semantic_uncertainty": {},
            }
            for index in range(60)
        ]
        episode = {
            "key": "episode-0001",
            "start_ms": 0,
            "end_ms": 59_800,
            "voice_clusters": [
                {
                    "label": "SPEAKER_00",
                    "identity_safe": False,
                    "contaminated": False,
                }
            ],
            "utterances": utterances,
            "asr_alternatives": [],
        }
        request = build_provider_request(
            self.session,
            [episode],
            settings=SemanticV2E02Settings(
                max_llm_request_chars=4_000,
                chunk_overlap_utterances=3,
            ),
        )
        self.assertGreater(len(request["jobs"]), 1)
        output = assemble_transport_episodes(request)
        self.assertEqual(len(output), 1)
        self.assertTrue(output[0]["complete_episode"])
        self.assertEqual(
            [item["id"] for item in output[0]["utterances"]],
            [item["key"] for item in utterances],
        )

    def test_all_low_information_tokens_still_produce_a_valid_audit_run(self) -> None:
        with patch(
            "allday_asr.application.semantic.current.OUTPUT_DIR", self.output_dir
        ):
            summary = run_semantic_v2e02(
                self.database,
                self.recording_id,
                asr_run_id=self.asr_run_id,
                diarization_run_id=self.diarization_run_id,
                settings=SemanticV2E02Settings(min_informative_chars=5),
            )
        self.assertEqual(summary.episode_count, 0)
        self.assertEqual(summary.excluded_block_count, 1)
        self.assertEqual(summary.candidate_count, 1)

    def _manual_response(
        self,
        request: dict,
        *,
        episode_key: str,
        start_ms: int,
        end_ms: int,
        first_key: str,
        second_key: str,
    ) -> dict:
        return {
            "format": SEMANTIC_RESPONSE_FORMAT,
            "request_sha256": self._request_sha(request),
            "mode": "codex_manual_eval",
            "daily_summary": {
                "title": "测试日摘要",
                "body": "识别出一段含现场与媒体证据的场景。",
                "evidence_scene_ids": ["scene-0001"],
            },
            "scenes": [
                {
                    "id": "scene-0001",
                    "episode_id": episode_key,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "title": "测试场景",
                    "summary": "母亲说话，随后出现媒体声音。",
                    "type": "mixed_context",
                    "participants": ["mother", "media"],
                    "confidence": 0.8,
                    "evidence_utterance_ids": [first_key, second_key],
                }
            ],
            "claims": [
                {
                    "id": "claim-0001",
                    "scene_id": "scene-0001",
                    "subject": "mother",
                    "claim_type": "personal_fact",
                    "text": "母亲说了你好。",
                    "confidence": 0.9,
                    "evidence_utterance_ids": [first_key],
                }
            ],
            "actions": [
                {
                    "id": "action-0001",
                    "scene_id": "scene-0001",
                    "owner": "unknown",
                    "text": "确认是否需要跟进。",
                    "due": None,
                    "confidence": 0.4,
                    "requires_human_confirmation": True,
                    "evidence_utterance_ids": [first_key],
                }
            ],
            "unresolved": [],
            "coverage": {"start_ms": start_ms, "end_ms": end_ms},
        }

    @staticmethod
    def _request_sha(request: dict) -> str:
        canonical = json.dumps(
            request,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _source_ref(self, start_ms: int, end_ms: int) -> dict:
        return {
            "source_object_id": int(self.source["source_object_id"]),
            "source_sha256": str(self.source["sha256"]),
            "source_start_ms": start_ms,
            "source_end_ms": end_ms,
        }

    def _create_truth_set(self) -> None:
        annotations = [
            {
                "annotation_key": "live-source",
                "annotation_kind": "speech",
                "session_start_ms": 1_000,
                "session_end_ms": 1_400,
                "label": "speech",
                "metadata": {"speech_source": "live_person"},
                "source_refs": [self._source_ref(1_000, 1_400)],
            },
            {
                "annotation_key": "mother-identity",
                "annotation_kind": "speaker",
                "session_start_ms": 1_000,
                "session_end_ms": 1_400,
                "label": "mother",
                "source_refs": [self._source_ref(1_000, 1_400)],
            },
            {
                "annotation_key": "media-source",
                "annotation_kind": "speech",
                "session_start_ms": 150_000,
                "session_end_ms": 150_400,
                "label": "speech",
                "metadata": {"speech_source": "media_playback"},
                "source_refs": [self._source_ref(150_000, 150_400)],
            },
        ]
        self.database.create_truth_set(
            {
                "truth_key": f"semantic-e02:{self.token}",
                "name": "semantic e02 test truth",
                "session_id": int(self.session["id"]),
                "format_version": "test-v1",
                "scope_start_ms": 0,
                "scope_end_ms": 200_000,
                "input_fingerprint": self.database.session_input_fingerprint(
                    int(self.session["id"])
                ),
                "truth_path": str(self.truth_path.resolve()),
                "truth_sha256": hashlib.sha256(self.truth_path.read_bytes()).hexdigest(),
            },
            annotations,
        )

    def _create_asr_run(self) -> int:
        run_id = self.database.start_processing_run(
            self.recording_id,
            run_kind="quality_asr_v2c",
            config={"test": True},
            config_sha256="semantic-e02-asr",
            model_manifest={"model_id": "fake/qwen"},
            pipeline_version="v2-c-test",
        )
        common = {
            "source_object_id": int(self.source["source_object_id"]),
            "source_sha256": str(self.source["sha256"]),
        }
        values = [
            ("你", 1_000, 1_200),
            ("好", 1_200, 1_400),
            ("再", 150_000, 150_200),
            ("见", 150_200, 150_400),
        ]
        self.database.create_asr_hypothesis(
            {
                "hypothesis_key": f"semantic-e02:{self.token}:primary",
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
                            **common,
                            "source_start_ms": start_ms,
                            "source_end_ms": end_ms,
                        }
                    ],
                }
                for index, (text, start_ms, end_ms) in enumerate(values)
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
            config_sha256="semantic-e02-diarization",
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
                    "attribution_key": f"semantic-e02:{self.token}:attr:{token['id']}",
                    "token_id": int(token["id"]),
                    "speaker_label": "SPEAKER_00" if index < 2 else None,
                    "attribution_kind": "primary" if index < 2 else "none",
                    "overlap_ms": (
                        int(token["session_end_ms"])
                        - int(token["session_start_ms"])
                        if index < 2
                        else 0
                    ),
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

    def _create_contamination_audit(self) -> None:
        run_id = self.database.start_processing_run(
            self.recording_id,
            run_kind="quality_diarization_v2d2",
            config={"test": True},
            config_sha256="semantic-e02-d2",
            model_manifest={"kind": "audit"},
            pipeline_version="v2-d.2-test",
            parent_run_id=self.diarization_run_id,
        )
        self.database.finish_processing_run(
            run_id,
            status="completed",
            summary={
                "diarization_run_id": self.diarization_run_id,
                "contaminated_speakers": ["SPEAKER_00"],
            },
            artifacts={},
        )


if __name__ == "__main__":
    unittest.main()
