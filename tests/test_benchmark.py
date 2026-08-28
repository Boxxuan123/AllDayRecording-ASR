from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import unittest
import wave
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import numpy as np

from allday_asr.asr.oracle_backends import OracleTranscript
from allday_asr.services.benchmark import (
    ACOUSTIC_BLIND_KIND,
    ACOUSTIC_BLIND_PROTOCOL_FORMAT,
    BLIND_PROTOCOL_FORMAT,
    COMPLETED_SUBSET_KIND,
    CONTINUOUS_TRUTH_FORMAT,
    SPEECH_SOURCE_MEDIA,
    _asr_metrics,
    benchmark_comparison,
    create_acoustic_blind_truth_task,
    create_blind_truth_task,
    create_continuous_truth_template,
    evaluate_benchmark,
    freeze_completed_blind_subset,
    import_continuous_truth,
    migrate_legacy_truth,
    normalize_text_itn_equivalent,
    paired_oracle_bootstrap,
    snapshot_oracle_asr_predictions,
    snapshot_v1_predictions,
)
from allday_asr.services.evaluation import EVALUATION_FORMAT, normalize_text
from allday_asr.storage.database import Database


class ContinuousBenchmarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parent
        self.token = uuid4().hex
        self.database_path = self.root / f"benchmark-{self.token}.sqlite3"
        self.output_dir = self.root / f"benchmark-output-{self.token}"
        self.database = Database(self.database_path)
        self.recording = self.database.create_recording(
            {
                "source_path": str((self.root / f"audio-{self.token}.m4a").resolve()),
                "sha256": f"benchmark-{self.token}",
                "device": "test-watch",
                "recorded_at": "2026-08-28T00:00:00+00:00",
                "timezone": "Asia/Singapore",
                "duration_ms": 5_000,
                "codec": "aac",
                "sample_rate": 16_000,
                "channels": 1,
                "bit_rate": 64_000,
                "encoder": "test",
            }
        )
        self.session = self.database.get_session_for_recording(int(self.recording["id"]))
        self.source = self.database.list_source_objects()[0]
        self.paths: list[Path] = []

    def tearDown(self) -> None:
        for path in self.paths:
            path.unlink(missing_ok=True)
        if self.output_dir.exists():
            shutil.rmtree(self.output_dir)
        for suffix in ("", "-shm", "-wal"):
            Path(f"{self.database_path}{suffix}").unlink(missing_ok=True)

    def test_exhaustive_metrics_and_immutable_snapshots(self) -> None:
        truth_path = self.root / f"continuous-{self.token}.jsonl"
        truth_path.write_text("{}\n", encoding="utf-8")
        self.paths.append(truth_path)
        annotations = [
            self._truth("speech", "speech", 1_000, 3_000, label="speech"),
            self._truth("transcript-a", "transcript", 1_000, 2_000, text="你好"),
            self._truth("transcript-b", "transcript", 2_000, 3_000, text="明天"),
            self._truth("speaker-a", "speaker", 1_000, 2_000, label="mother"),
            self._truth("speaker-b", "speaker", 2_000, 3_000, label="self"),
            self._truth("token", "alignment_token", 1_000, 1_200, text="你"),
            self._truth("entity", "entity", 1_000, 3_000, label="time", text="明天"),
        ]
        truth_set = self.database.create_truth_set(
            {
                "truth_key": f"truth:{self.token}",
                "name": "synthetic-exhaustive",
                "session_id": self.session["id"],
                "format_version": CONTINUOUS_TRUTH_FORMAT,
                "scope_start_ms": 0,
                "scope_end_ms": 5_000,
                "input_fingerprint": self.database.session_input_fingerprint(
                    int(self.session["id"])
                ),
                "completeness": {
                    "vad": "exhaustive",
                    "transcript": "exhaustive",
                    "speaker": "exhaustive",
                    "alignment": "exhaustive",
                    "entities": "exhaustive",
                },
                "truth_path": str(truth_path),
                "truth_sha256": hashlib.sha256(truth_path.read_bytes()).hexdigest(),
                "provenance": {"kind": "unit-test"},
            },
            annotations,
        )
        predictions = [
            self._prediction("speech", "speech", 1_500, 3_500, label="speech"),
            self._prediction("transcript", "transcript", 1_000, 3_000, text="你好明天"),
            self._prediction("speaker-x", "speaker", 1_000, 2_000, label="x"),
            self._prediction("speaker-y", "speaker", 2_000, 3_000, label="y"),
            self._prediction("token", "alignment_token", 1_050, 1_250, text="你"),
            self._prediction("entity", "entity", 1_000, 3_000, label="time", text="明天"),
        ]
        prediction_set = self.database.create_benchmark_prediction_set(
            {
                "prediction_key": f"prediction:{self.token}",
                "name": "perfect-except-vad-boundary",
                "session_id": self.session["id"],
                "input_fingerprint": self.database.session_input_fingerprint(
                    int(self.session["id"])
                ),
                "adapter": "unit-test",
                "model_manifest": {"model": "synthetic"},
            },
            predictions,
        )
        with patch(
            "allday_asr.services.benchmark.OUTPUT_DIR", self.output_dir
        ):
            summary = evaluate_benchmark(
                self.database, int(truth_set["id"]), int(prediction_set["id"])
            )

        self.assertEqual(summary.metrics["asr"]["cer"], 0)
        self.assertEqual(summary.metrics["vad"]["f1"], 0.75)
        self.assertEqual(summary.metrics["speaker"]["der"], 0)
        self.assertEqual(summary.metrics["speaker"]["jer"], 0)
        self.assertEqual(
            summary.metrics["alignment"]["mean_absolute_boundary_error_ms"], 50
        )
        self.assertEqual(summary.metrics["entities"]["extraction"]["f1"], 1)
        self.assertEqual(len(benchmark_comparison(self.database, int(truth_set["id"]))), 1)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE truth_annotations SET text = 'changed' WHERE truth_set_id = ?",
                    (truth_set["id"],),
                )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.connect() as connection:
                connection.execute(
                    "DELETE FROM benchmark_predictions WHERE prediction_set_id = ?",
                    (prediction_set["id"],),
                )

    def test_legacy_truth_migration_is_sparse_and_survives_vad_replacement(self) -> None:
        recording_id = int(self.recording["id"])
        self.database.replace_vad_segments(
            recording_id,
            [(1_000, 2_000), (3_000, 4_000)],
            str(self.recording["source_path"]),
        )
        segments = self.database.all_segments(recording_id)
        for segment, text in zip(segments, ["明天见", "好的"], strict=True):
            self.database.mark_segment_running(int(segment["id"]))
            self.database.mark_segment_completed(
                int(segment["id"]),
                language="zh",
                text_raw=text,
                text_display=text,
                asr_model="test-asr",
            )
        legacy_path = self.root / f"legacy-{self.token}.jsonl"
        continuous_path = self.root / f"legacy-{self.token}-v2.jsonl"
        self.paths.extend([legacy_path, continuous_path])
        legacy_rows = [
            {
                "type": "metadata",
                "format": EVALUATION_FORMAT,
                "name": "legacy",
                "recording_id": recording_id,
                "start_ms": 0,
                "end_ms": 5_000,
            },
            {
                "type": "segment",
                "include": True,
                "segment_id": int(segments[0]["id"]),
                "start_ms": 1_000,
                "end_ms": 2_000,
                "reference_text": "明天见",
                "reference_speaker": "self",
                "reference_identity": "self",
                "key_facts": ["明天"],
            },
            {
                "type": "segment",
                "include": False,
                "segment_id": int(segments[1]["id"]),
                "start_ms": 3_000,
                "end_ms": 4_000,
                "reference_text": "",
                "reference_speaker": "",
                "reference_identity": "",
                "key_facts": [],
            },
        ]
        legacy_path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in legacy_rows) + "\n",
            encoding="utf-8",
        )
        migrated = migrate_legacy_truth(
            self.database, legacy_path, output_path=continuous_path
        )
        truth_set = self.database.get_truth_set(migrated.truth_set_id)
        completeness = json.loads(str(truth_set["completeness_json"]))
        self.assertEqual(completeness["vad"], "sparse_positive_only")
        self.assertEqual(len(self.database.list_truth_annotations(migrated.truth_set_id)), 5)

        snapshot = snapshot_v1_predictions(
            self.database, migrated.truth_set_id, name=f"before-vad-change-{self.token}"
        )
        with patch(
            "allday_asr.services.benchmark.OUTPUT_DIR", self.output_dir
        ):
            benchmark = evaluate_benchmark(
                self.database, migrated.truth_set_id, snapshot.prediction_set_id
            )
        self.assertFalse(benchmark.metrics["vad"]["available"])
        self.assertFalse(benchmark.metrics["speaker"]["available"])
        self.assertEqual(benchmark.metrics["asr"]["cer"], 0)

        self.database.replace_vad_segments(
            recording_id, [(500, 4_500)], str(self.recording["source_path"])
        )
        annotations = self.database.list_truth_annotations(migrated.truth_set_id)
        self.assertEqual(len(annotations), 5)
        self.assertEqual(annotations[0]["session_start_ms"], 1_000)

    def test_new_truth_template_resolves_source_references_on_import(self) -> None:
        path = self.root / f"new-truth-{self.token}.jsonl"
        self.paths.append(path)
        create_continuous_truth_template(
            self.database,
            int(self.session["id"]),
            name=f"new-truth-{self.token}",
            start_ms=0,
            end_ms=2_000,
            output_path=path,
        )
        metadata = json.loads(path.read_text(encoding="utf-8").strip())
        metadata["completeness"]["vad"] = "exhaustive"
        annotation = {
            "type": "annotation",
            "key": "human:speech:0001",
            "kind": "speech",
            "session_start_ms": 500,
            "session_end_ms": 1_500,
            "label": "speech",
            "text": None,
            "metadata": {"reviewed": True},
        }
        path.write_text(
            json.dumps(metadata, ensure_ascii=False)
            + "\n"
            + json.dumps(annotation, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
        summary = import_continuous_truth(self.database, path)
        annotations = self.database.list_truth_annotations(summary.truth_set_id)
        sources = self.database.list_truth_annotation_sources(int(annotations[0]["id"]))
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["source_start_ms"], 500)
        self.assertEqual(sources[0]["source_end_ms"], 1_500)

    def test_blind_task_is_model_independent_and_cannot_freeze_while_pending(self) -> None:
        target = self.output_dir / "blind-task"

        def fake_materialize(window, destination, **_kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(f"window:{window.index}".encode())
            return destination

        with patch(
            "allday_asr.services.benchmark.materialize_logical_window",
            fake_materialize,
        ):
            summary = create_blind_truth_task(
                self.database,
                int(self.session["id"]),
                name=f"blind-{self.token}",
                duration_ms=4_000,
                chunk_ms=2_000,
                seed="fixed-test-seed",
                output_dir=target,
            )
        rows = [json.loads(line) for line in summary.task_path.read_text().splitlines()]
        self.assertEqual(rows[0]["provenance"]["protocol"], BLIND_PROTOCOL_FORMAT)
        self.assertFalse(rows[0]["provenance"]["model_outputs_used_for_selection"])
        self.assertNotIn("hypothesis_text_at_export", summary.task_path.read_text())
        with self.assertRaisesRegex(ValueError, "model_outputs_unseen"):
            import_continuous_truth(self.database, summary.task_path)

        rows[0]["completeness"]["vad"] = "exhaustive"
        rows[0]["completeness"]["transcript"] = "exhaustive"
        rows[0]["blind_attestation"] = {
            "model_outputs_unseen": True,
            "annotator": "unit-test-human",
            "completed_at": "2026-08-28T00:00:00Z",
        }
        for row in rows:
            if row.get("type") == "blind_window":
                row["review_status"] = "complete"
        rows.extend(
            [
                {
                    "type": "annotation",
                    "key": "blind:speech:0001",
                    "kind": "speech",
                    "session_start_ms": summary.scope_start_ms + 500,
                    "session_end_ms": summary.scope_start_ms + 1_500,
                    "label": "speech",
                    "text": None,
                    "metadata": {
                        "reviewed": True,
                        "utterance_id": "blind-0001",
                        "speech_source": SPEECH_SOURCE_MEDIA,
                    },
                },
                {
                    "type": "annotation",
                    "key": "blind:transcript:0001",
                    "kind": "transcript",
                    "session_start_ms": summary.scope_start_ms + 500,
                    "session_end_ms": summary.scope_start_ms + 1_500,
                    "label": None,
                    "text": "三十五一斤",
                    "metadata": {
                        "reviewed": True,
                        "utterance_id": "blind-0001",
                        "speech_source": SPEECH_SOURCE_MEDIA,
                    },
                },
            ]
        )
        summary.task_path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )
        frozen = import_continuous_truth(self.database, summary.task_path)
        self.assertEqual(frozen.annotation_count, 3)

    def test_v2c2_acoustic_blind_regions_exclude_unreviewed_gaps(self) -> None:
        target = self.output_dir / "blind-v2c2-task"
        scan_audio = self.root / f"acoustic-scan-{self.token}.wav"
        self.paths.append(scan_audio)
        sample_rate = 16_000
        seconds = []
        timeline = np.arange(sample_rate, dtype=np.float32) / sample_rate
        for index in range(5):
            if index in {1, 4}:
                signal = 0.22 * np.sin(2 * np.pi * 440 * timeline)
            else:
                signal = np.zeros(sample_rate, dtype=np.float32)
            seconds.append(np.round(signal * 32767).astype("<i2"))
        with wave.open(str(scan_audio), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            output.writeframes(np.concatenate(seconds).tobytes())

        @contextmanager
        def fake_full_window(_window):
            yield scan_audio

        def fake_materialize(window, destination, **_kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(f"window:{window.index}".encode())
            return destination

        with (
            patch(
                "allday_asr.services.benchmark.temporary_logical_window",
                fake_full_window,
            ),
            patch(
                "allday_asr.services.benchmark.materialize_logical_window",
                fake_materialize,
            ),
        ):
            summary = create_acoustic_blind_truth_task(
                self.database,
                int(self.session["id"]),
                name=f"blind-v2c2-{self.token}",
                review_duration_ms=2_000,
                chunk_ms=1_000,
                minimum_gap_ms=1_000,
                seed="fixed-acoustic-seed",
                output_dir=target,
            )
        rows = [json.loads(line) for line in summary.task_path.read_text().splitlines()]
        metadata = rows[0]
        self.assertEqual(metadata["provenance"]["kind"], ACOUSTIC_BLIND_KIND)
        self.assertEqual(
            metadata["provenance"]["protocol"], ACOUSTIC_BLIND_PROTOCOL_FORMAT
        )
        self.assertFalse(metadata["provenance"]["candidate_asr_outputs_used"])
        windows = [row for row in rows if row.get("type") == "blind_window"]
        self.assertEqual(len(windows), 2)
        self.assertEqual(
            [window["session_start_ms"] for window in windows], [1_000, 4_000]
        )
        self.assertGreaterEqual(
            windows[1]["session_start_ms"] - windows[0]["session_end_ms"], 1_000
        )
        self.assertTrue((target / "selection-manifest.json").is_file())

        partial_rows = json.loads(json.dumps(rows))
        partial_windows = [
            row for row in partial_rows if row.get("type") == "blind_window"
        ]
        partial_windows[0]["review_status"] = "complete"
        partial_start = int(partial_windows[0]["session_start_ms"]) + 200
        partial_end = int(partial_windows[0]["session_start_ms"]) + 800
        partial_rows.extend(
            [
                {
                    "type": "annotation",
                    "key": "blind:speech:partial-0001",
                    "kind": "speech",
                    "session_start_ms": partial_start,
                    "session_end_ms": partial_end,
                    "label": "speech",
                    "text": None,
                    "metadata": {
                        "reviewed": True,
                        "utterance_id": "partial-0001",
                        "window_index": 0,
                        "speech_source": SPEECH_SOURCE_MEDIA,
                    },
                },
                {
                    "type": "annotation",
                    "key": "blind:transcript:partial-0001",
                    "kind": "transcript",
                    "session_start_ms": partial_start,
                    "session_end_ms": partial_end,
                    "label": None,
                    "text": "电视节目",
                    "metadata": {
                        "reviewed": True,
                        "utterance_id": "partial-0001",
                        "window_index": 0,
                        "speech_source": SPEECH_SOURCE_MEDIA,
                    },
                },
            ]
        )
        partial_task = target / "partial-source.jsonl"
        partial_task.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in partial_rows)
            + "\n",
            encoding="utf-8",
        )
        partial_truth = freeze_completed_blind_subset(
            self.database,
            partial_task,
            name=f"partial-v2c2-{self.token}",
            output_path=target / "partial-frozen.jsonl",
        )
        partial_frozen_rows = [
            json.loads(line)
            for line in partial_truth.output_path.read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        self.assertEqual(
            partial_frozen_rows[0]["provenance"]["kind"], COMPLETED_SUBSET_KIND
        )
        self.assertEqual(
            partial_frozen_rows[0]["provenance"]["completed_window_indices"], [0]
        )
        self.assertEqual(partial_frozen_rows[0]["review_duration_ms"], 1_000)
        self.assertEqual(partial_truth.annotation_count, 3)

        metadata["completeness"]["vad"] = "exhaustive"
        metadata["completeness"]["transcript"] = "exhaustive"
        metadata["blind_attestation"] = {
            "model_outputs_unseen": True,
            "annotator": "unit-test-human",
            "completed_at": "2026-08-28T00:00:00Z",
        }
        for window in windows:
            window["review_status"] = "complete"
        first = windows[0]
        speech_start = int(first["session_start_ms"]) + 200
        speech_end = int(first["session_start_ms"]) + 800
        rows.extend(
            [
                {
                    "type": "annotation",
                    "key": "blind:speech:v2c2-0001",
                    "kind": "speech",
                    "session_start_ms": speech_start,
                    "session_end_ms": speech_end,
                    "label": "speech",
                    "text": None,
                    "metadata": {
                        "reviewed": True,
                        "utterance_id": "v2c2-0001",
                        "speech_source": SPEECH_SOURCE_MEDIA,
                    },
                },
                {
                    "type": "annotation",
                    "key": "blind:transcript:v2c2-0001",
                    "kind": "transcript",
                    "session_start_ms": speech_start,
                    "session_end_ms": speech_end,
                    "label": None,
                    "text": "吃饭对话",
                    "metadata": {
                        "reviewed": True,
                        "utterance_id": "v2c2-0001",
                        "speech_source": SPEECH_SOURCE_MEDIA,
                    },
                },
            ]
        )
        rows[-1]["metadata"]["speech_source"] = "telepathy"
        summary.task_path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "speech_source 无效"):
            import_continuous_truth(self.database, summary.task_path)
        rows[-1]["metadata"]["speech_source"] = SPEECH_SOURCE_MEDIA
        summary.task_path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )
        truth = import_continuous_truth(self.database, summary.task_path)
        imported = self.database.list_truth_annotations(truth.truth_set_id)
        transcript = next(
            row for row in imported if row["annotation_kind"] == "transcript"
        )
        self.assertEqual(
            json.loads(transcript["metadata_json"])["speech_source"],
            SPEECH_SOURCE_MEDIA,
        )
        self.assertEqual(truth.annotation_count, 4)

        gap_start = int(first["session_end_ms"])
        gap_end = int(windows[1]["session_start_ms"])
        predictions = [
            self._prediction(
                "inside-speech", "speech", speech_start, speech_end, label="speech"
            ),
            self._prediction(
                "inside-text", "transcript", speech_start, speech_end, text="吃饭对话"
            ),
            self._prediction(
                "gap-speech", "speech", gap_start, gap_end, label="speech"
            ),
            self._prediction(
                "gap-text", "transcript", gap_start, gap_end, text="不应计入"
            ),
        ]
        prediction_set = self.database.create_benchmark_prediction_set(
            {
                "prediction_key": f"v2c2-prediction:{self.token}",
                "name": f"v2c2-prediction-{self.token}",
                "session_id": self.session["id"],
                "input_fingerprint": self.database.session_input_fingerprint(
                    int(self.session["id"])
                ),
                "adapter": "unit-test",
                "model_manifest": {"model": "synthetic"},
            },
            predictions,
        )
        with patch("allday_asr.services.benchmark.OUTPUT_DIR", self.output_dir):
            benchmark = evaluate_benchmark(
                self.database, truth.truth_set_id, int(prediction_set["id"])
            )
        self.assertEqual(benchmark.metrics["asr"]["cer"], 0)
        self.assertEqual(benchmark.metrics["asr"]["orphan_hypotheses"], 0)
        self.assertEqual(benchmark.metrics["vad"]["false_positive_ms"], 0)
        self.assertEqual(benchmark.metrics["vad"]["scope_ms"], 2_000)
        with self.assertRaisesRegex(ValueError, "跨越非连续 review-region"):
            _asr_metrics(
                [
                    self._truth(
                        "scope-ref",
                        "transcript",
                        speech_start,
                        speech_end,
                        text="吃饭对话",
                    )
                ],
                [
                    self._prediction(
                        "crossing-text",
                        "transcript",
                        speech_start,
                        int(first["session_end_ms"]) + 100,
                        text="越界内容",
                    )
                ],
                normalizer=normalize_text,
                scope=[
                    (
                        int(window["session_start_ms"]),
                        int(window["session_end_ms"]),
                    )
                    for window in windows
                ],
                exhaustive=True,
            )

    def test_oracle_snapshots_dual_cer_and_paired_bootstrap(self) -> None:
        path = self.root / f"oracle-truth-{self.token}.jsonl"
        self.paths.append(path)
        create_continuous_truth_template(
            self.database,
            int(self.session["id"]),
            name=f"oracle-truth-{self.token}",
            start_ms=0,
            end_ms=2_000,
            output_path=path,
        )
        metadata = json.loads(path.read_text(encoding="utf-8"))
        annotation = {
            "type": "annotation",
            "key": "human:transcript:0001",
            "kind": "transcript",
            "session_start_ms": 500,
            "session_end_ms": 1_500,
            "text": "35一斤",
            "metadata": {
                "reviewed": True,
                "speech_source": SPEECH_SOURCE_MEDIA,
            },
        }
        path.write_text(
            json.dumps(metadata, ensure_ascii=False)
            + "\n"
            + json.dumps(annotation, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
        truth = import_continuous_truth(self.database, path)

        class FakeBackend:
            model_revision = "test-revision"

            def __init__(self, model_id: str, text: str):
                self.model_id = model_id
                self.backend_name = "unit-test-oracle"
                self.text = text
                self.closed = False

            def transcribe(self, _audio_path, *, language):
                return OracleTranscript(
                    text=self.text,
                    language=language,
                    raw_response={"text": self.text},
                )

            def parameters(self):
                return {"segmentation": "human-reference-interval"}

            def close(self):
                self.closed = True

        @contextmanager
        def fake_window(_window):
            yield self.root / "not-read-by-fake.wav"

        baseline_backend = FakeBackend("baseline", "35一斤")
        candidate_backend = FakeBackend("candidate", "三十五一斤")
        with patch(
            "allday_asr.services.benchmark.temporary_logical_window", fake_window
        ):
            baseline = snapshot_oracle_asr_predictions(
                self.database,
                truth.truth_set_id,
                baseline_backend,
                name=f"baseline-{self.token}",
            )
            candidate = snapshot_oracle_asr_predictions(
                self.database,
                truth.truth_set_id,
                candidate_backend,
                name=f"candidate-{self.token}",
            )
        self.assertTrue(baseline_backend.closed)
        self.assertTrue(candidate_backend.closed)
        with patch("allday_asr.services.benchmark.OUTPUT_DIR", self.output_dir):
            candidate_result = evaluate_benchmark(
                self.database, truth.truth_set_id, candidate.prediction_set_id
            )
        self.assertGreater(candidate_result.metrics["asr"]["cer"], 0)
        self.assertEqual(candidate_result.metrics["asr_itn"]["cer"], 0)
        paired = paired_oracle_bootstrap(
            self.database,
            truth.truth_set_id,
            baseline.prediction_set_id,
            candidate.prediction_set_id,
            samples=100,
        )
        self.assertGreater(paired["candidate_minus_baseline_cer"], 0)
        paired_media = paired_oracle_bootstrap(
            self.database,
            truth.truth_set_id,
            baseline.prediction_set_id,
            candidate.prediction_set_id,
            samples=100,
            speech_source=SPEECH_SOURCE_MEDIA,
        )
        self.assertEqual(paired_media["evaluated_intervals"], 1)
        self.assertEqual(paired_media["speech_source"], SPEECH_SOURCE_MEDIA)
        with self.assertRaisesRegex(ValueError, "speech_source 无效"):
            paired_oracle_bootstrap(
                self.database,
                truth.truth_set_id,
                baseline.prediction_set_id,
                candidate.prediction_set_id,
                samples=10,
                speech_source="telepathy",
            )
        paired_itn = paired_oracle_bootstrap(
            self.database,
            truth.truth_set_id,
            baseline.prediction_set_id,
            candidate.prediction_set_id,
            samples=100,
            itn_equivalent=True,
        )
        self.assertEqual(paired_itn["candidate_minus_baseline_cer"], 0)

    def test_itn_equivalent_normalization_is_conservative(self) -> None:
        self.assertEqual(normalize_text_itn_equivalent("35 一斤"), "35一斤")
        self.assertEqual(normalize_text_itn_equivalent("三十五一斤"), "35一斤")
        self.assertEqual(normalize_text_itn_equivalent("十分好"), "十分好")
        self.assertEqual(normalize_text_itn_equivalent("二零二六年"), "二零二六年")
        self.assertEqual(normalize_text_itn_equivalent("一亿种可能"), "一亿种可能")

    def test_exhaustive_asr_counts_orphans_and_respects_unintelligible_masks(self) -> None:
        references = [self._truth("ref", "transcript", 500, 1_500, text="你好")]
        predictions = [
            self._prediction("matched", "transcript", 500, 1_500, text="你好"),
            self._prediction("orphan", "transcript", 3_000, 4_000, text="幻觉"),
        ]
        sparse = _asr_metrics(
            references,
            predictions,
            normalizer=normalize_text,
            scope=(0, 5_000),
            exhaustive=False,
        )
        self.assertEqual(sparse["errors"], 0)
        exhaustive = _asr_metrics(
            references,
            predictions,
            normalizer=normalize_text,
            scope=(0, 5_000),
            exhaustive=True,
        )
        self.assertEqual(exhaustive["insertions"], 2)
        self.assertEqual(exhaustive["orphan_hypotheses"], 1)
        masked = _asr_metrics(
            [
                *references,
                self._truth(
                    "unclear",
                    "uncertain",
                    3_000,
                    4_000,
                    label="unintelligible",
                ),
            ],
            predictions,
            normalizer=normalize_text,
            scope=(0, 5_000),
            exhaustive=True,
        )
        self.assertEqual(masked["errors"], 0)
        self.assertEqual(masked["excluded_uncertain_ranges"], 1)

    def _source_ref(self, start_ms: int, end_ms: int) -> list[dict]:
        return [
            {
                "source_object_id": int(self.source["id"]),
                "source_sha256": str(self.source["sha256"]),
                "source_start_ms": start_ms,
                "source_end_ms": end_ms,
            }
        ]

    def _truth(
        self,
        key: str,
        kind: str,
        start_ms: int,
        end_ms: int,
        *,
        label: str | None = None,
        text: str | None = None,
    ) -> dict:
        return {
            "annotation_key": key,
            "annotation_kind": kind,
            "session_start_ms": start_ms,
            "session_end_ms": end_ms,
            "label": label,
            "text": text,
            "metadata": {},
            "source_refs": self._source_ref(start_ms, end_ms),
        }

    @staticmethod
    def _prediction(
        key: str,
        kind: str,
        start_ms: int,
        end_ms: int,
        *,
        label: str | None = None,
        text: str | None = None,
    ) -> dict:
        return {
            "prediction_key": key,
            "prediction_kind": kind,
            "session_start_ms": start_ms,
            "session_end_ms": end_ms,
            "label": label,
            "text": text,
            "metadata": {},
        }


if __name__ == "__main__":
    unittest.main()
