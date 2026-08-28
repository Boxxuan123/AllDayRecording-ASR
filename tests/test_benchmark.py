from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from allday_asr.services.benchmark import (
    CONTINUOUS_TRUTH_FORMAT,
    benchmark_comparison,
    create_continuous_truth_template,
    evaluate_benchmark,
    import_continuous_truth,
    migrate_legacy_truth,
    snapshot_v1_predictions,
)
from allday_asr.services.evaluation import EVALUATION_FORMAT
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
