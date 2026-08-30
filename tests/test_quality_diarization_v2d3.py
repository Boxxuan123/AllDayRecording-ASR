from __future__ import annotations

import hashlib
import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import numpy as np
import soundfile as sf

from allday_asr.services.benchmark import (
    CONTINUOUS_TRUTH_FORMAT,
    import_continuous_truth,
)
from allday_asr.services.quality_diarization_v2d3 import (
    EMBEDDING_WINDOW_SAMPLES,
    V2D3Settings,
    build_candidate_windows,
    pack_seed_waveforms,
    review_identity_candidate,
    run_identity_candidate_mining,
    score_identity_candidates,
    sync_identity_reference_set,
)
from allday_asr.services.speaker_timeline import speaker_timeline_overview
from allday_asr.storage.database import Database


class FakeCommunityEmbeddingBackend:
    model_id = "fake/community-1"
    model_revision = "test-revision"
    backend_name = "fake-community-embedding"

    def __init__(self) -> None:
        self.calls = 0

    def ensure_loaded(self) -> None:
        return None

    def parameters(self) -> dict:
        return {"fake": True}

    def extract_speaker_embeddings(
        self, samples: list[np.ndarray], *, batch_size: int
    ) -> np.ndarray:
        del batch_size
        self.calls += 1
        self._assert_windows(samples)
        if self.calls == 1:
            # father, mother and tv weak seeds
            return np.asarray(((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0)), dtype=np.float32)
        values = ((0.95, 0.05), (-0.95, 0.05))
        return np.asarray(values[: len(samples)], dtype=np.float32)

    def _assert_windows(self, samples: list[np.ndarray]) -> None:
        for samples_item in samples:
            if len(samples_item) != EMBEDDING_WINDOW_SAMPLES:
                raise AssertionError("embedding windows must have fixed length")


class QualityDiarizationV2D3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parent
        self.token = uuid4().hex
        self.database_path = self.root / f"v2d3-{self.token}.sqlite3"
        self.source_path = self.root / f"v2d3-{self.token}.wav"
        self.truth_path = self.root / f"v2d3-truth-{self.token}.jsonl"
        self.output_dir = self.root / f"v2d3-output-{self.token}"
        samples = np.zeros(18 * 16_000, dtype=np.float32)
        samples[0::97] = 0.2
        sf.write(self.source_path, samples, 16_000, subtype="PCM_16")
        source_bytes = self.source_path.read_bytes()
        self.source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        self.database = Database.open(self.database_path)
        recording = self.database.create_recording(
            {
                "source_path": str(self.source_path.resolve()),
                "sha256": self.source_sha256,
                "device": "watch",
                "recorded_at": "2026-08-29T00:00:00+00:00",
                "timezone": "Asia/Singapore",
                "duration_ms": 18_000,
                "codec": "pcm_s16le",
                "sample_rate": 16_000,
                "channels": 1,
                "bit_rate": 256_000,
                "encoder": "test",
            }
        )
        self.recording_id = int(recording["id"])
        self.database.update_recording(
            self.recording_id,
            normalized_path=str(self.source_path.resolve()),
            status="completed",
        )
        self.session = self.database.get_session_for_recording(self.recording_id)
        self.source = self.database.list_session_sources(int(self.session["id"]))[0]
        self.diarization_run_id = self._create_diarization_run()
        self.truth_set_id = self._create_truth()

    def tearDown(self) -> None:
        if self.output_dir.exists():
            shutil.rmtree(self.output_dir)
        self.source_path.unlink(missing_ok=True)
        self.truth_path.unlink(missing_ok=True)
        for suffix in ("", "-shm", "-wal"):
            Path(f"{self.database_path}{suffix}").unlink(missing_ok=True)
        for backup in self.root.glob(f"v2d3-{self.token}.schema-*.sqlite3"):
            backup.unlink(missing_ok=True)

    def test_weak_seed_run_ranks_but_never_assigns_identity(self) -> None:
        backend = FakeCommunityEmbeddingBackend()
        with patch(
            "allday_asr.application.diarization.identity_candidates.OUTPUT_DIR",
            self.output_dir,
        ):
            summary = run_identity_candidate_mining(
                self.database,
                None,
                session_id=int(self.session["id"]),
                diarization_run_id=self.diarization_run_id,
                truth_set_id=self.truth_set_id,
                target_identity="father",
                settings=V2D3Settings(max_candidates=2),
                embedding_backend=backend,
            )

        self.assertEqual(summary.seed_quality, "weak")
        self.assertEqual(summary.target_truth_ms, 3_000)
        self.assertEqual(summary.target_embedding_count, 1)
        self.assertEqual(summary.selected_candidates, 2)
        payload = json.loads(summary.manifest_path.read_text(encoding="utf-8"))
        self.assertFalse(payload["safety"]["identity_assignments_written"])
        self.assertFalse(payload["safety"]["biometric_embeddings_persisted"])
        candidates = payload["summary"]["candidates"]
        self.assertEqual(candidates[0]["speaker"], "SPEAKER_FATHER_LIKE")
        self.assertGreater(candidates[0]["contrastive_margin"], 0.8)
        self.assertFalse(candidates[0]["auto_assigned"])
        sealed_summary = self.database.get_processing_run(summary.run_id)["summary_json"]
        review = review_identity_candidate(
            self.database,
            summary.run_id,
            candidate_id=candidates[0]["id"],
            status="confirmed_target",
        )
        self.assertEqual(review["status"], "confirmed_target")
        self.assertEqual(
            review["reference_set"]["status"], "provisional_reference_set"
        )
        self.assertEqual(review["reference_set"]["confirmed_intervals"], 2)
        self.assertEqual(review["reference_set"]["confirmed_duration_ms"], 6_000)
        references = self.database.list_identity_reference_intervals("father")
        self.assertEqual(len(references), 2)
        self.assertEqual(
            {row["provenance_kind"] for row in references},
            {"truth", "v2d3_review"},
        )
        self.assertEqual(
            {row["source_sha256"] for row in references}, {self.source_sha256}
        )
        self.assertEqual(
            self.database.get_processing_run(summary.run_id)["summary_json"],
            sealed_summary,
        )
        with patch(
            "allday_asr.application.diarization.identity_candidates.OUTPUT_DIR",
            self.output_dir,
        ):
            repeated = run_identity_candidate_mining(
                self.database,
                None,
                session_id=int(self.session["id"]),
                diarization_run_id=self.diarization_run_id,
                truth_set_id=self.truth_set_id,
                target_identity="father",
                settings=V2D3Settings(max_candidates=2),
                embedding_backend=FakeCommunityEmbeddingBackend(),
            )
        inherited = self.database.list_identity_candidate_reviews(repeated.run_id)
        self.assertEqual(len(inherited), 1)
        self.assertEqual(inherited[0]["status"], "confirmed_target")
        reference_summary = sync_identity_reference_set(self.database, "father")
        self.assertEqual(reference_summary.confirmed_intervals, 2)
        self.assertEqual(reference_summary.confirmed_duration_ms, 6_000)
        self.assertEqual(reference_summary.sessions, 1)
        self.assertEqual(reference_summary.source_objects, 1)
        self.assertEqual(reference_summary.source_reference_rows, 2)
        overview = speaker_timeline_overview(self.database, self.recording_id)
        self.assertTrue(overview["v2d3"]["available"])
        self.assertEqual(overview["queues"]["identity_expansion"]["count"], 2)
        self.assertEqual(overview["v2d3"]["reviewed_candidates"], 1)
        self.assertEqual(
            overview["queues"]["identity_expansion"]["items"][0]["review_status"],
            "confirmed_target",
        )
        changed = review_identity_candidate(
            self.database,
            repeated.run_id,
            candidate_id=candidates[0]["id"],
            status="rejected",
        )
        self.assertEqual(changed["reference_set"]["confirmed_intervals"], 1)
        self.assertEqual(changed["reference_set"]["confirmed_duration_ms"], 3_000)
        self.assertEqual(changed["reference_set"]["rejected_intervals"], 1)
        updated_references = self.database.list_identity_reference_intervals(
            "father"
        )
        self.assertEqual(len(updated_references), 2)
        self.assertEqual(
            [row["decision"] for row in updated_references].count("rejected"), 1
        )
        self.assertEqual(hashlib.sha256(self.source_path.read_bytes()).hexdigest(), self.source_sha256)

    def test_seed_packing_candidate_exclusion_and_contrastive_order(self) -> None:
        packed = pack_seed_waveforms(
            [np.ones(20_000, dtype=np.float32), np.ones(30_000, dtype=np.float32)]
        )
        self.assertEqual(len(packed), 2)
        self.assertEqual({len(item) for item in packed}, {EMBEDDING_WINDOW_SAMPLES})
        rows = [
            {
                "id": 1,
                "session_start_ms": 0,
                "session_end_ms": 800,
                "speaker_label": "A",
            },
            {
                "id": 2,
                "session_start_ms": 900,
                "session_end_ms": 2_000,
                "speaker_label": "A",
            },
            {
                "id": 3,
                "session_start_ms": 4_000,
                "session_end_ms": 6_000,
                "speaker_label": "B",
            },
        ]
        truth = [{"session_start_ms": 4_500, "session_end_ms": 5_000}]
        windows = build_candidate_windows(
            rows, truth, settings=V2D3Settings(truth_guard_ms=100)
        )
        self.assertEqual([(item["start_ms"], item["end_ms"]) for item in windows], [(0, 2_000)])
        scored = score_identity_candidates(
            [
                {"id": "high", "start_ms": 0},
                {"id": "medium", "start_ms": 1},
                {"id": "exploratory", "start_ms": 2},
                {"id": "negative", "start_ms": 3},
            ],
            np.asarray(
                ((1.0, 0.0), (0.31, 0.27), (0.25, 0.05), (0.0, 1.0)),
                dtype=np.float32,
            ),
            {
                "father": np.asarray((1.0, 0.0), dtype=np.float32),
                "tv": np.asarray((0.0, 1.0), dtype=np.float32),
            },
            target_identity="father",
        )
        self.assertEqual(
            [item["id"] for item in scored],
            ["high", "medium", "exploratory", "negative"],
        )
        self.assertGreater(scored[0]["contrastive_margin"], 0)

    def _create_diarization_run(self) -> int:
        run_id = self.database.start_processing_run(
            self.recording_id,
            run_kind="quality_diarization_v2d",
            config={"test": True},
            config_sha256="v2d3-diarization",
            model_manifest={"model_id": "fake/community-1"},
            pipeline_version="v2-d",
        )
        turns = []
        for index, (start_ms, end_ms, speaker) in enumerate(
            (
                (10_000, 13_000, "SPEAKER_FATHER_LIKE"),
                (14_000, 17_000, "SPEAKER_TV_LIKE"),
            )
        ):
            turns.append(
                {
                    "turn_key": f"v2d3:{self.token}:{index}",
                    "session_id": int(self.session["id"]),
                    "turn_index": index,
                    "turn_kind": "exclusive",
                    "speaker_label": speaker,
                    "session_start_ms": start_ms,
                    "session_end_ms": end_ms,
                    "source_refs": [
                        {
                            "source_object_id": int(self.source["source_object_id"]),
                            "source_sha256": str(self.source["sha256"]),
                            "source_start_ms": start_ms,
                            "source_end_ms": end_ms,
                        }
                    ],
                }
            )
        self.database.create_diarization_turns(
            run_id, int(self.session["id"]), turns
        )
        self.database.finish_processing_run(
            run_id,
            status="completed",
            summary={"asr_run_id": None},
            artifacts={},
        )
        return run_id

    def _create_truth(self) -> int:
        rows = [
            {
                "type": "metadata",
                "format": CONTINUOUS_TRUTH_FORMAT,
                "name": f"v2d3-identities-{self.token}",
                "session_id": int(self.session["id"]),
                "scope_start_ms": 0,
                "scope_end_ms": 9_000,
                "input_fingerprint": self.database.session_input_fingerprint(
                    int(self.session["id"])
                ),
                "completeness": {
                    "vad": "none",
                    "transcript": "none",
                    "speaker": "sparse",
                    "identity": "sparse",
                    "overlap": "none",
                    "alignment": "none",
                    "entities": "none",
                },
                "provenance": {"kind": "synthetic-v2d3-test"},
            },
            *[
                {
                    "type": "annotation",
                    "key": f"identity:{identity}",
                    "kind": "speaker",
                    "session_start_ms": start_ms,
                    "session_end_ms": end_ms,
                    "label": identity,
                    "text": None,
                    "metadata": {"reviewed": True},
                }
                for identity, start_ms, end_ms in (
                    ("father", 0, 3_000),
                    ("mother", 3_000, 6_000),
                    ("tv", 6_000, 9_000),
                )
            ],
        ]
        self.truth_path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )
        return import_continuous_truth(self.database, self.truth_path).truth_set_id


if __name__ == "__main__":
    unittest.main()
