from __future__ import annotations

import hashlib
import json
import sqlite3
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from allday_asr.asr.quality_backends import AlignedToken, HypothesisResult
from allday_asr.services.quality_asr import (
    QualityAsrSettings,
    run_quality_asr,
    snapshot_quality_asr,
)
from allday_asr.storage.database import Database


class FakeBackend:
    backend_name = "fake"
    model_revision = "test-revision"

    def __init__(self, role: str, text: str):
        self.role = role
        self.model_id = f"fake/{role}"
        self.alignment_model_id = f"fake/{role}-aligner"
        self.text = text
        self.closed = False

    def transcribe(self, audio_path: Path, *, language: str | None):
        del audio_path
        return HypothesisResult(
            text=self.text,
            language=language,
            tokens=(
                AlignedToken(self.text[0], 0.100, 0.300),
                AlignedToken(self.text[1], 0.300, 0.500),
            ),
            raw_response={
                "text": self.text,
                "speech_ranges_ms": [[100, 500]],
                "segments": [],
            },
        )

    def parameters(self):
        return {"fake": True}

    def close(self):
        self.closed = True


class GateFakeBackend(FakeBackend):
    def transcribe(self, audio_path: Path, *, language: str | None):
        del audio_path
        return HypothesisResult(
            text=self.text[0],
            language=language,
            tokens=(
                AlignedToken(
                    self.text[0],
                    0.100,
                    0.300,
                    metadata={
                        "speech_gate_accepted": True,
                        "speech_gate_committed": True,
                    },
                ),
                AlignedToken(
                    self.text[1],
                    0.300,
                    0.500,
                    metadata={
                        "speech_gate_accepted": False,
                        "speech_gate_committed": False,
                    },
                ),
            ),
            raw_response={
                "text": self.text,
                "committed_token_text": self.text[0],
                "speech_ranges_ms": [[100, 300]],
                "segments": [{"accepted": True}, {"accepted": False}],
            },
        )


class QualityAsrTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parent
        self.token = uuid4().hex
        self.database_path = self.root / f"quality-{self.token}.sqlite3"
        self.source_path = self.root / f"quality-{self.token}.m4a"
        self.output_dir = self.root / f"quality-output-{self.token}"
        self.truth_path = self.root / f"quality-truth-{self.token}.jsonl"
        self.source_path.write_bytes(b"immutable-watch-audio")
        self.database = Database(self.database_path)
        digest = hashlib.sha256(self.source_path.read_bytes()).hexdigest()
        recording = self.database.create_recording(
            {
                "source_path": str(self.source_path.resolve()),
                "sha256": digest,
                "device": "watch",
                "recorded_at": "2026-08-28T00:00:00+00:00",
                "timezone": "Asia/Singapore",
                "duration_ms": 2_000,
                "codec": "aac",
                "sample_rate": 16_000,
                "channels": 1,
                "bit_rate": 64_000,
                "encoder": "test",
            }
        )
        self.recording_id = int(recording["id"])

    def tearDown(self) -> None:
        for path in self.output_dir.glob("**/*") if self.output_dir.exists() else []:
            if path.is_file():
                path.unlink()
        for path in sorted(
            self.output_dir.glob("**/*") if self.output_dir.exists() else [], reverse=True
        ):
            if path.is_dir():
                path.rmdir()
        self.output_dir.rmdir() if self.output_dir.exists() else None
        self.source_path.unlink(missing_ok=True)
        self.truth_path.unlink(missing_ok=True)
        for suffix in ("", "-shm", "-wal"):
            Path(f"{self.database_path}{suffix}").unlink(missing_ok=True)
        for backup in self.root.glob(f"quality-{self.token}.schema-*.sqlite3"):
            backup.unlink(missing_ok=True)

    def test_dual_hypotheses_tokens_are_source_traced_and_snapshot_is_frozen(self):
        @contextmanager
        def fake_window(window):
            del window
            yield self.source_path

        settings = QualityAsrSettings(window_ms=1_000, context_ms=100)
        with (
            patch("allday_asr.services.quality_asr.temporary_logical_window", fake_window),
            patch("allday_asr.services.quality_asr.OUTPUT_DIR", self.output_dir),
        ):
            summary = run_quality_asr(
                self.database,
                self.recording_id,
                settings=settings,
                primary_factory=lambda: FakeBackend("primary", "你好"),
                secondary_factory=lambda: FakeBackend("secondary", "您好"),
            )

        self.assertEqual(summary.primary_hypotheses, 2)
        self.assertEqual(summary.secondary_hypotheses, 2)
        self.assertEqual(summary.aligned_tokens, 8)
        self.assertEqual(summary.disagreements, 2)
        hypotheses = self.database.list_asr_hypotheses(summary.run_id)
        for hypothesis in hypotheses:
            for token in self.database.list_asr_tokens(int(hypothesis["id"])):
                sources = self.database.list_asr_token_sources(int(token["id"]))
                self.assertTrue(sources)
                self.assertEqual(
                    sum(row["source_end_ms"] - row["source_start_ms"] for row in sources),
                    token["session_end_ms"] - token["session_start_ms"],
                )

        snapshot = snapshot_quality_asr(self.database, summary.run_id)
        prediction_set = self.database.get_benchmark_prediction_set(
            snapshot.prediction_set_id
        )
        self.assertEqual(prediction_set["status"], "frozen")
        predictions = self.database.list_benchmark_predictions(
            snapshot.prediction_set_id
        )
        transcript_predictions = [
            row for row in predictions if row["prediction_kind"] == "transcript"
        ]
        alignment_predictions = [
            row for row in predictions if row["prediction_kind"] == "alignment_token"
        ]
        speech_predictions = [
            row for row in predictions if row["prediction_kind"] == "speech"
        ]
        primary_core_tokens = [
            token
            for hypothesis in hypotheses
            if hypothesis["hypothesis_role"] == "primary"
            for token in self.database.list_asr_tokens(
                int(hypothesis["id"]), core_only=True
            )
        ]
        self.assertEqual(len(transcript_predictions), len(primary_core_tokens))
        self.assertEqual(len(alignment_predictions), len(primary_core_tokens))
        self.assertEqual(len(speech_predictions), 2)
        self.assertEqual(
            snapshot.prediction_count,
            2 * len(primary_core_tokens) + len(speech_predictions),
        )
        self.assertEqual(
            {
                (row["session_start_ms"], row["session_end_ms"], row["text"])
                for row in transcript_predictions
            },
            {
                (row["session_start_ms"], row["session_end_ms"], row["text"])
                for row in primary_core_tokens
            },
        )

        session = self.database.get_session_for_recording(self.recording_id)
        source = self.database.list_session_sources(int(session["id"]))[0]
        self.truth_path.write_text('{"unit_test":true}\n', encoding="utf-8")
        source_common = {
            "source_object_id": int(source["source_object_id"]),
            "source_sha256": str(source["sha256"]),
        }
        truth_set = self.database.create_truth_set(
            {
                "truth_key": f"quality-scopes:{self.token}",
                "name": f"quality-scopes-{self.token}",
                "session_id": int(session["id"]),
                "format_version": "unit-test",
                "scope_start_ms": 0,
                "scope_end_ms": 1_200,
                "input_fingerprint": self.database.session_input_fingerprint(
                    int(session["id"])
                ),
                "completeness": {"vad": "exhaustive", "transcript": "exhaustive"},
                "truth_path": str(self.truth_path),
                "truth_sha256": hashlib.sha256(
                    self.truth_path.read_bytes()
                ).hexdigest(),
                "provenance": {"kind": "unit-test"},
            },
            [
                {
                    "annotation_key": "scope-0",
                    "annotation_kind": "uncertain",
                    "session_start_ms": 0,
                    "session_end_ms": 250,
                    "label": "review_region_complete_scope",
                    "source_refs": [
                        {**source_common, "source_start_ms": 0, "source_end_ms": 250}
                    ],
                },
                {
                    "annotation_key": "scope-1",
                    "annotation_kind": "uncertain",
                    "session_start_ms": 1_100,
                    "session_end_ms": 1_200,
                    "label": "review_region_complete_scope",
                    "source_refs": [
                        {
                            **source_common,
                            "source_start_ms": 1_100,
                            "source_end_ms": 1_200,
                        }
                    ],
                },
            ],
        )
        scoped_snapshot = snapshot_quality_asr(
            self.database,
            summary.run_id,
            name=f"scoped-{self.token}",
            truth_set_id=int(truth_set["id"]),
        )
        scoped_set = self.database.get_benchmark_prediction_set(
            scoped_snapshot.prediction_set_id
        )
        scoped_predictions = self.database.list_benchmark_predictions(
            scoped_snapshot.prediction_set_id, prediction_kind="transcript"
        )
        scoped_speech = self.database.list_benchmark_predictions(
            scoped_snapshot.prediction_set_id, prediction_kind="speech"
        )
        self.assertEqual(
            [
                (row["session_start_ms"], row["session_end_ms"])
                for row in scoped_predictions
            ],
            [(100, 250), (1_100, 1_200)],
        )
        self.assertEqual(
            [
                (row["session_start_ms"], row["session_end_ms"])
                for row in scoped_speech
            ],
            [(100, 250), (1_100, 1_200)],
        )
        self.assertTrue(
            all(
                json.loads(row["metadata_json"])["scope_clipped"]
                for row in scoped_predictions
            )
        )
        self.assertEqual(
            json.loads(scoped_set["model_manifest_json"])["benchmark_scope"][
                "truth_set_id"
            ],
            int(truth_set["id"]),
        )

        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE asr_hypotheses SET text = 'changed' WHERE id = ?",
                    (hypotheses[0]["id"],),
                )

    def test_v2c3_keeps_rejected_context_tokens_as_evidence_not_predictions(self):
        @contextmanager
        def fake_window(window):
            del window
            yield self.source_path

        with (
            patch("allday_asr.services.quality_asr.temporary_logical_window", fake_window),
            patch("allday_asr.services.quality_asr.OUTPUT_DIR", self.output_dir),
        ):
            summary = run_quality_asr(
                self.database,
                self.recording_id,
                settings=QualityAsrSettings(window_ms=1_000, context_ms=100),
                primary_factory=lambda: GateFakeBackend("primary", "你好"),
                secondary_factory=lambda: FakeBackend("secondary", "您好"),
            )

        primary = self.database.list_asr_hypotheses(
            summary.run_id, role="primary"
        )
        self.assertEqual(summary.speech_candidates, 4)
        self.assertEqual(summary.accepted_speech_candidates, 2)
        self.assertEqual(summary.rejected_speech_candidates, 2)
        self.assertEqual(summary.committed_primary_tokens, 2)
        self.assertEqual(
            sum(len(self.database.list_asr_tokens(int(row["id"]))) for row in primary),
            4,
        )
        self.assertEqual(
            sum(
                len(self.database.list_asr_tokens(int(row["id"]), core_only=True))
                for row in primary
            ),
            2,
        )


if __name__ == "__main__":
    unittest.main()
