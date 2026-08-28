from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Sequence

from allday_asr.paths import EVALUATION_DIR, OUTPUT_DIR
from allday_asr.services.evaluation import (
    EVALUATION_FORMAT,
    NAME_PATTERN,
    levenshtein_operations,
    normalize_text,
)
from allday_asr.services.sources import resolve_session_slices
from allday_asr.storage.database import Database


CONTINUOUS_TRUTH_FORMAT = "AllDayRecording continuous truth v2"
BENCHMARK_REPORT_FORMAT = "AllDayRecording continuous benchmark report v1"
EXHAUSTIVE = "exhaustive"


@dataclass(frozen=True)
class ContinuousTruthSummary:
    truth_set_id: int
    session_id: int
    annotation_count: int
    output_path: Path
    truth_sha256: str


@dataclass(frozen=True)
class PredictionSnapshotSummary:
    prediction_set_id: int
    session_id: int
    prediction_count: int
    content_sha256: str


@dataclass(frozen=True)
class BenchmarkSummary:
    benchmark_run_id: int
    truth_set_id: int
    prediction_set_id: int
    metrics: dict[str, Any]
    report_json_path: Path
    report_markdown_path: Path


def create_continuous_truth_template(
    database: Database,
    session_id: int,
    *,
    name: str,
    start_ms: int = 0,
    end_ms: int | None = None,
    output_path: Path | None = None,
) -> Path:
    if not NAME_PATTERN.fullmatch(name):
        raise ValueError("name 只能包含字母、数字、点、下划线和连字符，最长 64 字符")
    session = database.get_recording_session(session_id)
    effective_end = int(session["duration_ms"]) if end_ms is None else end_ms
    if start_ms < 0 or effective_end <= start_ms:
        raise ValueError("连续时间真值范围无效")
    if effective_end > int(session["duration_ms"]):
        raise ValueError("连续时间真值范围超过录音会话")
    target = (
        output_path.resolve()
        if output_path is not None
        else EVALUATION_DIR
        / f"session-{session_id:06d}"
        / f"{name}-continuous-v2.jsonl"
    )
    metadata = {
        "type": "metadata",
        "format": CONTINUOUS_TRUTH_FORMAT,
        "name": name,
        "session_id": session_id,
        "scope_start_ms": start_ms,
        "scope_end_ms": effective_end,
        "input_fingerprint": database.session_input_fingerprint(session_id),
        "completeness": {
            "vad": "none",
            "transcript": "none",
            "speaker": "none",
            "alignment": "none",
            "entities": "none",
        },
        "provenance": {
            "kind": "human_continuous_annotation",
            "warning": (
                "Set a task to exhaustive only after the entire scope has been reviewed."
            ),
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_jsonl_atomically(target, [metadata])
    return target


def migrate_legacy_truth(
    database: Database,
    legacy_path: Path,
    *,
    name: str | None = None,
    output_path: Path | None = None,
) -> ContinuousTruthSummary:
    legacy_path = legacy_path.resolve(strict=True)
    rows = _read_jsonl(legacy_path)
    if not rows or rows[0].get("type") != "metadata":
        raise ValueError("旧真值文件第一行必须是 metadata")
    metadata = rows[0]
    if metadata.get("format") != EVALUATION_FORMAT:
        raise ValueError(f"不支持的旧真值格式：{metadata.get('format')}")
    recording_id = int(metadata["recording_id"])
    session = database.get_session_for_recording(recording_id)
    session_id = int(session["id"])
    scope_start = int(metadata.get("start_ms", 0))
    scope_end = int(metadata.get("end_ms", session["duration_ms"]))
    if scope_start < 0 or scope_end <= scope_start:
        raise ValueError("旧真值的连续时间范围无效")
    if scope_end > int(session["duration_ms"]):
        raise ValueError("旧真值范围超过录音会话")

    truth_name = name or f"{metadata.get('name') or legacy_path.stem}-continuous-v2"
    if not NAME_PATTERN.fullmatch(truth_name):
        raise ValueError("连续时间真值名称无效")
    target = (
        output_path.resolve()
        if output_path is not None
        else legacy_path.with_name(f"{legacy_path.stem}-continuous-v2.jsonl")
    )
    if target.exists():
        raise FileExistsError(f"连续时间真值已存在，不会覆盖：{target}")

    current_segments = {
        int(row["id"]): row for row in database.all_segments(recording_id)
    }
    annotations: list[dict[str, Any]] = []
    excluded = 0
    for legacy in rows[1:]:
        if legacy.get("type") != "segment":
            continue
        if not legacy.get("include", True):
            excluded += 1
            continue
        segment_id = int(legacy["segment_id"])
        start_ms = int(legacy["start_ms"])
        end_ms = int(legacy["end_ms"])
        if end_ms <= start_ms or start_ms < scope_start or end_ms > scope_end:
            raise ValueError(f"旧片段 {segment_id} 的时间范围无效")
        current = current_segments.get(segment_id)
        boundary_status = "exported_time_only"
        if (
            current is not None
            and int(current["start_ms"]) == start_ms
            and int(current["end_ms"]) == end_ms
        ):
            boundary_status = "matches_current_v1_segment"
        source_refs = _source_refs(database, session_id, start_ms, end_ms)
        common = {
            "legacy_segment_id": segment_id,
            "notes": str(legacy.get("notes", "")),
            "boundary_status": boundary_status,
            "provenance": "legacy_v1_segment_annotation",
        }
        base = f"legacy-segment-{segment_id}"
        annotations.append(
            _annotation(
                f"{base}:speech",
                "speech",
                start_ms,
                end_ms,
                label="speech",
                metadata=common,
                legacy_segment_id=segment_id,
                source_refs=source_refs,
            )
        )
        reference_text = str(legacy.get("reference_text", "")).strip()
        if reference_text:
            annotations.append(
                _annotation(
                    f"{base}:transcript",
                    "transcript",
                    start_ms,
                    end_ms,
                    text=reference_text,
                    metadata=common,
                    legacy_segment_id=segment_id,
                    source_refs=source_refs,
                )
            )
        speaker = str(legacy.get("reference_speaker", "")).strip()
        if speaker:
            annotations.append(
                _annotation(
                    f"{base}:speaker:{speaker}",
                    "speaker",
                    start_ms,
                    end_ms,
                    label=speaker,
                    metadata=common,
                    legacy_segment_id=segment_id,
                    source_refs=source_refs,
                )
            )
        identity = str(legacy.get("reference_identity", "")).strip().lower()
        if identity in {"self", "not_self"}:
            annotations.append(
                _annotation(
                    f"{base}:identity",
                    "identity",
                    start_ms,
                    end_ms,
                    label=identity,
                    metadata=common,
                    legacy_segment_id=segment_id,
                    source_refs=source_refs,
                )
            )
        facts = legacy.get("key_facts", [])
        if not isinstance(facts, list):
            raise ValueError(f"旧片段 {segment_id} 的 key_facts 必须是列表")
        for index, fact in enumerate(facts):
            fact_text = str(fact).strip()
            if fact_text:
                annotations.append(
                    _annotation(
                        f"{base}:entity:{index}",
                        "entity",
                        start_ms,
                        end_ms,
                        label="key_fact",
                        text=fact_text,
                        metadata=common,
                        legacy_segment_id=segment_id,
                        source_refs=source_refs,
                    )
                )

    if not annotations:
        raise RuntimeError("旧真值中没有可以迁移的 include=true 标注")
    input_fingerprint = database.session_input_fingerprint(session_id)
    completeness = {
        "vad": "sparse_positive_only",
        "transcript": "sparse",
        "speaker": "sparse_segment_labels",
        "alignment": "none",
        "entities": "sparse",
    }
    continuous_metadata = {
        "type": "metadata",
        "format": CONTINUOUS_TRUTH_FORMAT,
        "name": truth_name,
        "session_id": session_id,
        "recording_id": recording_id,
        "scope_start_ms": scope_start,
        "scope_end_ms": scope_end,
        "input_fingerprint": input_fingerprint,
        "completeness": completeness,
        "provenance": {
            "kind": "legacy_v1_jsonl_migration",
            "legacy_truth_path": str(legacy_path),
            "legacy_truth_sha256": _sha256_file(legacy_path),
            "excluded_legacy_segments": excluded,
            "warning": (
                "VAD and diarization coverage are not exhaustive because the source "
                "annotations were created from V1 VAD segments."
            ),
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    serialized_rows = [continuous_metadata] + [
        {
            "type": "annotation",
            "key": item["annotation_key"],
            "kind": item["annotation_kind"],
            "session_start_ms": item["session_start_ms"],
            "session_end_ms": item["session_end_ms"],
            "label": item.get("label"),
            "text": item.get("text"),
            "metadata": item.get("metadata", {}),
            "legacy_segment_id": item.get("legacy_segment_id"),
            "source_refs": item.get("source_refs", []),
        }
        for item in annotations
    ]
    _write_jsonl_atomically(target, serialized_rows)
    return import_continuous_truth(database, target)


def import_continuous_truth(
    database: Database, truth_path: Path
) -> ContinuousTruthSummary:
    truth_path = truth_path.resolve(strict=True)
    rows = _read_jsonl(truth_path)
    if not rows or rows[0].get("type") != "metadata":
        raise ValueError("连续时间真值第一行必须是 metadata")
    metadata = rows[0]
    if metadata.get("format") != CONTINUOUS_TRUTH_FORMAT:
        raise ValueError(f"不支持的连续时间真值格式：{metadata.get('format')}")
    session_id = int(metadata["session_id"])
    session = database.get_recording_session(session_id)
    scope_start = int(metadata["scope_start_ms"])
    scope_end = int(metadata["scope_end_ms"])
    if scope_start < 0 or scope_end <= scope_start:
        raise ValueError("连续时间真值范围无效")
    if scope_end > int(session["duration_ms"]):
        raise ValueError("连续时间真值范围超过录音会话")
    expected_fingerprint = database.session_input_fingerprint(session_id)
    if metadata.get("input_fingerprint") != expected_fingerprint:
        raise ValueError("连续时间真值绑定的原始输入指纹与当前会话不一致")
    completeness = metadata.get("completeness", {})
    if not isinstance(completeness, dict):
        raise ValueError("completeness 必须是对象")

    annotations: list[dict[str, Any]] = []
    truth_name = str(metadata.get("name", ""))
    if not NAME_PATTERN.fullmatch(truth_name):
        raise ValueError("连续时间真值名称无效")
    keys: set[str] = set()
    for row in rows[1:]:
        if row.get("type") != "annotation":
            continue
        key = str(row["key"])
        if key in keys:
            raise ValueError(f"连续时间真值包含重复 annotation key：{key}")
        keys.add(key)
        start_ms = int(row["session_start_ms"])
        end_ms = int(row["session_end_ms"])
        if start_ms < scope_start or end_ms > scope_end or end_ms <= start_ms:
            raise ValueError(f"标注 {key} 超出真值范围")
        expected_refs = _source_refs(database, session_id, start_ms, end_ms)
        supplied_refs = row.get("source_refs")
        if supplied_refs is not None and _canonical_refs(supplied_refs) != _canonical_refs(
            expected_refs
        ):
            raise ValueError(f"标注 {key} 的原始音频范围引用不正确")
        annotations.append(
            _annotation(
                key,
                str(row["kind"]),
                start_ms,
                end_ms,
                label=_optional_text(row.get("label")),
                text=_optional_text(row.get("text")),
                metadata=_annotation_metadata(row, key),
                legacy_segment_id=row.get("legacy_segment_id"),
                source_refs=expected_refs,
            )
        )
    if not annotations:
        raise RuntimeError("连续时间真值没有 annotation")

    truth_sha256 = _sha256_file(truth_path)
    truth_key = f"continuous:{session_id}:{truth_sha256}"
    truth_set = database.create_truth_set(
        {
            "truth_key": truth_key,
            "name": truth_name,
            "session_id": session_id,
            "format_version": CONTINUOUS_TRUTH_FORMAT,
            "scope_start_ms": scope_start,
            "scope_end_ms": scope_end,
            "input_fingerprint": expected_fingerprint,
            "completeness": completeness,
            "truth_path": str(truth_path),
            "truth_sha256": truth_sha256,
            "provenance": metadata.get("provenance", {}),
        },
        annotations,
    )
    return ContinuousTruthSummary(
        truth_set_id=int(truth_set["id"]),
        session_id=session_id,
        annotation_count=len(annotations),
        output_path=truth_path,
        truth_sha256=truth_sha256,
    )


def snapshot_v1_predictions(
    database: Database,
    truth_set_id: int,
    *,
    name: str,
    processing_run_id: int | None = None,
) -> PredictionSnapshotSummary:
    if not NAME_PATTERN.fullmatch(name):
        raise ValueError("预测快照名称无效")
    truth_set = database.get_truth_set(truth_set_id)
    session = database.get_recording_session(int(truth_set["session_id"]))
    session_id = int(session["id"])
    input_fingerprint = database.session_input_fingerprint(session_id)
    if str(truth_set["input_fingerprint"]) != input_fingerprint:
        raise ValueError("冻结真值的输入指纹与当前录音会话不一致")
    recording_id = session["legacy_recording_id"]
    if recording_id is None:
        raise RuntimeError("V1 预测适配器只支持带 legacy_recording_id 的会话")
    runs = database.list_processing_runs(int(recording_id))
    if processing_run_id is None and runs:
        processing_run_id = int(runs[-1]["id"])
    if processing_run_id is not None:
        matching = [row for row in runs if int(row["id"]) == processing_run_id]
        if not matching:
            raise ValueError(f"processing run {processing_run_id} 不属于当前录音")
        run_fingerprint = matching[0]["input_fingerprint"]
        if run_fingerprint and str(run_fingerprint) != input_fingerprint:
            raise ValueError("processing run 的原始输入与当前会话不一致")

    self_profile = database.get_self_profile()
    self_id = int(self_profile["id"]) if self_profile is not None else None
    predictions: list[dict[str, Any]] = []
    asr_models: set[str] = set()
    for segment in database.all_segments(int(recording_id)):
        segment_id = int(segment["id"])
        start_ms = int(segment["start_ms"])
        end_ms = int(segment["end_ms"])
        common = {
            "legacy_segment_id": segment_id,
            "asr_status": str(segment["asr_status"]),
        }
        predictions.append(
            _prediction(
                f"segment-{segment_id}:speech",
                "speech",
                start_ms,
                end_ms,
                label="speech",
                metadata=common,
            )
        )
        text = str(segment["text_display"] or "").strip()
        if text:
            if segment["asr_model"]:
                asr_models.add(str(segment["asr_model"]))
            predictions.append(
                _prediction(
                    f"segment-{segment_id}:transcript",
                    "transcript",
                    start_ms,
                    end_ms,
                    text=text,
                    metadata={**common, "asr_model": segment["asr_model"]},
                )
            )
        speaker = segment["person_name"] or segment["speaker_session_id"]
        if speaker:
            predictions.append(
                _prediction(
                    f"segment-{segment_id}:speaker",
                    "speaker",
                    start_ms,
                    end_ms,
                    label=str(speaker),
                    metadata=common,
                )
            )
        if segment["person_id"] is not None:
            identity = "self" if int(segment["person_id"]) == self_id else "not_self"
            predictions.append(
                _prediction(
                    f"segment-{segment_id}:identity",
                    "identity",
                    start_ms,
                    end_ms,
                    label=identity,
                    confidence=segment["speaker_match_score"],
                    metadata=common,
                )
            )

    canonical = json.dumps(
        predictions, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    content_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    prediction_set = database.create_benchmark_prediction_set(
        {
            "prediction_key": f"v1:{session_id}:{name}:{content_sha256}",
            "name": name,
            "session_id": session_id,
            "processing_run_id": processing_run_id,
            "input_fingerprint": input_fingerprint,
            "adapter": "v1-current-database-snapshot",
            "model_manifest": {
                "asr_models": sorted(asr_models),
                "source": "speech_segments",
            },
            "content_sha256": content_sha256,
        },
        predictions,
    )
    return PredictionSnapshotSummary(
        prediction_set_id=int(prediction_set["id"]),
        session_id=session_id,
        prediction_count=len(predictions),
        content_sha256=content_sha256,
    )


def evaluate_benchmark(
    database: Database,
    truth_set_id: int,
    prediction_set_id: int,
) -> BenchmarkSummary:
    truth_set = database.get_truth_set(truth_set_id)
    prediction_set = database.get_benchmark_prediction_set(prediction_set_id)
    if int(truth_set["session_id"]) != int(prediction_set["session_id"]):
        raise ValueError("真值集与预测快照不属于同一录音会话")
    if str(truth_set["input_fingerprint"]) != str(
        prediction_set["input_fingerprint"]
    ):
        raise ValueError("真值集与预测快照的原始输入指纹不一致")
    annotations = database.list_truth_annotations(truth_set_id)
    predictions = database.list_benchmark_predictions(prediction_set_id)
    completeness = json.loads(str(truth_set["completeness_json"]))
    scope = (int(truth_set["scope_start_ms"]), int(truth_set["scope_end_ms"]))

    metrics = {
        "asr": _asr_metrics(annotations, predictions),
        "vad": (
            _vad_metrics(annotations, predictions, scope)
            if completeness.get("vad") == EXHAUSTIVE
            else _unavailable(
                "truth coverage is not exhaustive; VAD misses and false alarms would be biased"
            )
        ),
        "speaker": (
            _speaker_metrics(annotations, predictions, scope)
            if completeness.get("speaker") == EXHAUSTIVE
            else _unavailable(
                "speaker activity coverage is not exhaustive; DER/JER would be biased"
            )
        ),
        "alignment": (
            _alignment_metrics(annotations, predictions)
            if completeness.get("alignment") == EXHAUSTIVE
            else _unavailable("token timing truth is not exhaustive")
        ),
        "entities": _entity_metrics(annotations, predictions),
    }
    config = {
        "format": BENCHMARK_REPORT_FORMAT,
        "time_unit": "milliseconds",
        "vad_collar_ms": 0,
        "speaker_overlap": "included",
        "asr_normalization": "NFKC + lowercase + remove whitespace/punctuation",
    }
    details = {
        "truth_completeness": completeness,
        "truth_annotation_count": len(annotations),
        "prediction_count": len(predictions),
    }
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = (
        OUTPUT_DIR
        / f"session-{int(truth_set['session_id']):06d}"
        / "benchmarks"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"truth-{truth_set_id}-prediction-{prediction_set_id}-{timestamp}"
    report_json = output_dir / f"{stem}.json"
    report_markdown = output_dir / f"{stem}.md"
    payload = {
        "format": BENCHMARK_REPORT_FORMAT,
        "truth_set_id": truth_set_id,
        "prediction_set_id": prediction_set_id,
        "truth_sha256": truth_set["truth_sha256"],
        "prediction_sha256": prediction_set["content_sha256"],
        "input_fingerprint": truth_set["input_fingerprint"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "config": config,
        "metrics": metrics,
        "details": details,
    }
    report_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report_markdown.write_text(_render_report(payload), encoding="utf-8")
    run_id = database.record_benchmark_run(
        truth_set_id,
        prediction_set_id,
        config=config,
        metrics=metrics,
        details=details,
        report_json_path=str(report_json),
        report_markdown_path=str(report_markdown),
    )
    return BenchmarkSummary(
        benchmark_run_id=run_id,
        truth_set_id=truth_set_id,
        prediction_set_id=prediction_set_id,
        metrics=metrics,
        report_json_path=report_json,
        report_markdown_path=report_markdown,
    )


def benchmark_comparison(database: Database, truth_set_id: int) -> list[dict[str, Any]]:
    comparison: list[dict[str, Any]] = []
    for row in database.list_benchmark_runs(truth_set_id):
        metrics = json.loads(str(row["metrics_json"]))
        comparison.append(
            {
                "benchmark_run_id": int(row["id"]),
                "prediction_set_id": int(row["prediction_set_id"]),
                "prediction_name": str(row["prediction_name"]),
                "adapter": str(row["prediction_adapter"]),
                "asr_cer": metrics["asr"].get("cer"),
                "vad_f1": metrics["vad"].get("f1"),
                "der": metrics["speaker"].get("der"),
                "jer": metrics["speaker"].get("jer"),
                "alignment_mean_ms": metrics["alignment"].get(
                    "mean_absolute_boundary_error_ms"
                ),
                "entity_f1": metrics["entities"]["extraction"].get("f1"),
            }
        )
    return comparison


def _asr_metrics(annotations: Sequence[Any], predictions: Sequence[Any]) -> dict[str, Any]:
    references = _of_kind(annotations, "transcript")
    hypotheses = _of_kind(predictions, "transcript")
    totals = {"reference_chars": 0, "substitutions": 0, "deletions": 0, "insertions": 0}
    exact = 0
    details: list[dict[str, Any]] = []
    components = _transcript_components(references, hypotheses)
    for component_references, component_hypotheses in components:
        component_references.sort(key=lambda row: (_start(row), _end(row)))
        component_hypotheses.sort(key=lambda row: (_start(row), _end(row)))
        reference_text = "".join(
            str(_value(row, "text") or "") for row in component_references
        )
        hypothesis_text = "".join(
            str(_value(row, "text") or "") for row in component_hypotheses
        )
        normalized_reference = normalize_text(reference_text)
        normalized_hypothesis = normalize_text(hypothesis_text)
        operations = levenshtein_operations(
            normalized_reference, normalized_hypothesis
        )
        totals["reference_chars"] += len(normalized_reference)
        for key in ("substitutions", "deletions", "insertions"):
            totals[key] += operations[key]
        exact += normalized_reference == normalized_hypothesis
        details.append(
            {
                "annotation_keys": [
                    _value(row, "annotation_key") for row in component_references
                ],
                "start_ms": min(_start(row) for row in component_references),
                "end_ms": max(_end(row) for row in component_references),
                "reference_text": reference_text,
                "hypothesis_text": hypothesis_text,
                "operations": operations,
            }
        )
    errors = totals["substitutions"] + totals["deletions"] + totals["insertions"]
    return {
        "available": bool(references),
        "evaluated_intervals": len(references),
        "evaluation_components": len(components),
        **totals,
        "errors": errors,
        "cer": _safe_ratio(errors, totals["reference_chars"]),
        "exact_match_rate": _safe_ratio(exact, len(components)),
        "intervals": details,
    }


def _transcript_components(
    references: Sequence[Any], hypotheses: Sequence[Any]
) -> list[tuple[list[Any], list[Any]]]:
    components: list[tuple[list[Any], list[Any]]] = []
    remaining_references = set(range(len(references)))
    while remaining_references:
        seed = min(remaining_references)
        reference_indexes = {seed}
        hypothesis_indexes: set[int] = set()
        queue: list[tuple[str, int]] = [("reference", seed)]
        while queue:
            kind, index = queue.pop()
            if kind == "reference":
                reference = references[index]
                for hypothesis_index, hypothesis in enumerate(hypotheses):
                    if (
                        hypothesis_index not in hypothesis_indexes
                        and _overlap_ms(
                            hypothesis, _start(reference), _end(reference)
                        )
                        > 0
                    ):
                        hypothesis_indexes.add(hypothesis_index)
                        queue.append(("hypothesis", hypothesis_index))
            else:
                hypothesis = hypotheses[index]
                for reference_index, reference in enumerate(references):
                    if (
                        reference_index not in reference_indexes
                        and _overlap_ms(
                            reference, _start(hypothesis), _end(hypothesis)
                        )
                        > 0
                    ):
                        reference_indexes.add(reference_index)
                        queue.append(("reference", reference_index))
        remaining_references -= reference_indexes
        components.append(
            (
                [references[index] for index in sorted(reference_indexes)],
                [hypotheses[index] for index in sorted(hypothesis_indexes)],
            )
        )
    return components


def _vad_metrics(
    annotations: Sequence[Any], predictions: Sequence[Any], scope: tuple[int, int]
) -> dict[str, Any]:
    references = _of_kind(annotations, "speech")
    hypotheses = _of_kind(predictions, "speech")
    atoms = _time_atoms(scope, references, hypotheses)
    tp = fp = fn = tn = 0
    for start, end in atoms:
        midpoint = (start + end) / 2
        reference_active = _has_active(references, midpoint)
        hypothesis_active = _has_active(hypotheses, midpoint)
        duration = end - start
        if reference_active and hypothesis_active:
            tp += duration
        elif reference_active:
            fn += duration
        elif hypothesis_active:
            fp += duration
        else:
            tn += duration
    precision = _safe_ratio(tp, tp + fp)
    recall = _safe_ratio(tp, tp + fn)
    ref_boundaries = sorted(
        {_start(row) for row in references} | {_end(row) for row in references}
    )
    hyp_boundaries = sorted(
        {_start(row) for row in hypotheses} | {_end(row) for row in hypotheses}
    )
    boundary_errors = [
        min(abs(boundary - candidate) for candidate in hyp_boundaries)
        for boundary in ref_boundaries
        if hyp_boundaries
    ]
    return {
        "available": True,
        "scope_ms": scope[1] - scope[0],
        "true_positive_ms": tp,
        "false_positive_ms": fp,
        "false_negative_ms": fn,
        "true_negative_ms": tn,
        "miss_rate": _safe_ratio(fn, tp + fn),
        "false_alarm_rate": _safe_ratio(fp, fp + tn),
        "precision": precision,
        "recall": recall,
        "f1": _f1(precision, recall),
        "mean_boundary_error_ms": (
            sum(boundary_errors) / len(boundary_errors) if boundary_errors else None
        ),
    }


def _speaker_metrics(
    annotations: Sequence[Any], predictions: Sequence[Any], scope: tuple[int, int]
) -> dict[str, Any]:
    references = [row for row in _of_kind(annotations, "speaker") if _label(row)]
    hypotheses = [row for row in _of_kind(predictions, "speaker") if _label(row)]
    atoms = _time_atoms(scope, references, hypotheses)
    reference_labels = sorted({_label(row) for row in references})
    hypothesis_labels = sorted({_label(row) for row in hypotheses})
    overlap_weights: dict[tuple[str, str], float] = Counter()
    atom_activity: list[tuple[int, set[str], set[str]]] = []
    for start, end in atoms:
        midpoint = (start + end) / 2
        ref_active = _active_labels(references, midpoint)
        hyp_active = _active_labels(hypotheses, midpoint)
        duration = end - start
        atom_activity.append((duration, ref_active, hyp_active))
        for hypothesis in hyp_active:
            for reference in ref_active:
                overlap_weights[(hypothesis, reference)] += duration
    mapping = _best_assignment(hypothesis_labels, reference_labels, overlap_weights)
    totals = _der_totals(atom_activity, mapping)
    overlap_atoms = [item for item in atom_activity if len(item[1]) > 1]
    overlap_totals = _der_totals(overlap_atoms, mapping)

    ref_duration = {
        label: sum(duration for duration, refs, _ in atom_activity if label in refs)
        for label in reference_labels
    }
    hyp_duration = {
        label: sum(duration for duration, _, hyps in atom_activity if label in hyps)
        for label in hypothesis_labels
    }
    iou_weights: dict[tuple[str, str], float] = {}
    for reference in reference_labels:
        for hypothesis in hypothesis_labels:
            intersection = overlap_weights.get((hypothesis, reference), 0.0)
            union = ref_duration[reference] + hyp_duration[hypothesis] - intersection
            iou_weights[(reference, hypothesis)] = intersection / union if union else 0.0
    jer_mapping = _best_assignment(reference_labels, hypothesis_labels, iou_weights)
    per_speaker_jer = [
        1.0 - iou_weights.get((reference, jer_mapping.get(reference, "")), 0.0)
        for reference in reference_labels
    ]
    return {
        "available": True,
        "reference_speakers": len(reference_labels),
        "hypothesis_speakers": len(hypothesis_labels),
        "mapping": mapping,
        **totals,
        "der": _safe_ratio(
            totals["miss_ms"] + totals["false_alarm_ms"] + totals["confusion_ms"],
            totals["reference_speaker_ms"],
        ),
        "jer": sum(per_speaker_jer) / len(per_speaker_jer) if per_speaker_jer else None,
        "overlap": {
            **overlap_totals,
            "der": _safe_ratio(
                overlap_totals["miss_ms"]
                + overlap_totals["false_alarm_ms"]
                + overlap_totals["confusion_ms"],
                overlap_totals["reference_speaker_ms"],
            ),
        },
    }


def _alignment_metrics(
    annotations: Sequence[Any], predictions: Sequence[Any]
) -> dict[str, Any]:
    references = _of_kind(annotations, "alignment_token")
    hypotheses = _of_kind(predictions, "alignment_token")
    if not references:
        return _unavailable("truth set has no alignment tokens")
    used: set[int] = set()
    boundary_errors: list[int] = []
    start_errors: list[int] = []
    end_errors: list[int] = []
    matched = 0
    for reference in references:
        token = normalize_text(str(_value(reference, "text") or _label(reference)))
        reference_index = _metadata(reference).get("token_index")
        candidates: list[int] = []
        for index, hypothesis in enumerate(hypotheses):
            if index in used:
                continue
            hypothesis_token = normalize_text(
                str(_value(hypothesis, "text") or _label(hypothesis))
            )
            if hypothesis_token == token:
                hypothesis_index = _metadata(hypothesis).get("token_index")
                if reference_index is None or hypothesis_index == reference_index:
                    candidates.append(index)
        candidate_index = (
            min(
                candidates,
                key=lambda index: abs(_start(hypotheses[index]) - _start(reference)),
            )
            if candidates
            else None
        )
        if candidate_index is None:
            continue
        used.add(candidate_index)
        hypothesis = hypotheses[candidate_index]
        start_error = abs(_start(reference) - _start(hypothesis))
        end_error = abs(_end(reference) - _end(hypothesis))
        start_errors.append(start_error)
        end_errors.append(end_error)
        boundary_errors.extend([start_error, end_error])
        matched += 1
    return {
        "available": True,
        "reference_tokens": len(references),
        "matched_tokens": matched,
        "failed_tokens": len(references) - matched,
        "failure_rate": _safe_ratio(len(references) - matched, len(references)),
        "mean_start_error_ms": _mean(start_errors),
        "mean_end_error_ms": _mean(end_errors),
        "mean_absolute_boundary_error_ms": _mean(boundary_errors),
        "p95_absolute_boundary_error_ms": _percentile(boundary_errors, 0.95),
    }


def _entity_metrics(
    annotations: Sequence[Any], predictions: Sequence[Any]
) -> dict[str, Any]:
    references = _of_kind(annotations, "entity")
    hypotheses = _of_kind(predictions, "entity")
    reference_counter = Counter(_entity_key(row) for row in references)
    hypothesis_counter = Counter(_entity_key(row) for row in hypotheses)
    true_positive = sum((reference_counter & hypothesis_counter).values())
    reference_total = sum(reference_counter.values())
    hypothesis_total = sum(hypothesis_counter.values())
    precision = _safe_ratio(true_positive, hypothesis_total)
    recall = _safe_ratio(true_positive, reference_total)

    transcript_predictions = _of_kind(predictions, "transcript")
    preserved = 0
    for reference in references:
        start, end = _interval(reference)
        hypothesis_text = "".join(
            str(_value(row, "text") or "")
            for row in transcript_predictions
            if _overlap_ms(row, start, end) > 0
        )
        entity_text = normalize_text(str(_value(reference, "text") or ""))
        if entity_text and entity_text in normalize_text(hypothesis_text):
            preserved += 1
    return {
        "available": bool(references),
        "reference_entities": reference_total,
        "extraction": {
            "available": bool(hypotheses),
            "predicted_entities": hypothesis_total,
            "true_positive": true_positive,
            "false_positive": hypothesis_total - true_positive,
            "false_negative": reference_total - true_positive,
            "precision": precision,
            "recall": recall,
            "f1": _f1(precision, recall),
            "reason": None if hypotheses else "prediction set has no entity extractor output",
        },
        "transcript_preservation": {
            "matched": preserved,
            "recall": _safe_ratio(preserved, reference_total),
        },
    }


def _der_totals(
    atoms: Sequence[tuple[int, set[str], set[str]]], mapping: dict[str, str]
) -> dict[str, int]:
    miss = false_alarm = confusion = reference_speaker = 0
    for duration, references, hypotheses in atoms:
        mapped = {mapping.get(label, f"__unmapped__:{label}") for label in hypotheses}
        correct = len(references & mapped)
        miss += max(0, len(references) - len(hypotheses)) * duration
        false_alarm += max(0, len(hypotheses) - len(references)) * duration
        confusion += (min(len(references), len(hypotheses)) - correct) * duration
        reference_speaker += len(references) * duration
    return {
        "reference_speaker_ms": reference_speaker,
        "miss_ms": miss,
        "false_alarm_ms": false_alarm,
        "confusion_ms": confusion,
    }


def _best_assignment(
    left_labels: Sequence[str],
    right_labels: Sequence[str],
    weights: dict[tuple[str, str], float],
) -> dict[str, str]:
    if not left_labels or not right_labels:
        return {}
    if len(right_labels) > 16:
        available = set(right_labels)
        result: dict[str, str] = {}
        for left in left_labels:
            if not available:
                break
            right = max(available, key=lambda item: weights.get((left, item), 0.0))
            if weights.get((left, right), 0.0) > 0:
                result[left] = right
                available.remove(right)
        return result

    @lru_cache(maxsize=None)
    def solve(position: int, used_mask: int) -> tuple[float, tuple[int, ...]]:
        if position == len(left_labels):
            return 0.0, ()
        best_score, best_path = solve(position + 1, used_mask)
        best_path = (-1,) + best_path
        left = left_labels[position]
        for index, right in enumerate(right_labels):
            if used_mask & (1 << index):
                continue
            tail_score, tail_path = solve(position + 1, used_mask | (1 << index))
            score = weights.get((left, right), 0.0) + tail_score
            if score > best_score:
                best_score = score
                best_path = (index,) + tail_path
        return best_score, best_path

    _, path = solve(0, 0)
    return {
        left_labels[position]: right_labels[index]
        for position, index in enumerate(path)
        if index >= 0 and weights.get((left_labels[position], right_labels[index]), 0.0) > 0
    }


def _time_atoms(
    scope: tuple[int, int], *row_groups: Sequence[Any]
) -> list[tuple[int, int]]:
    boundaries = {scope[0], scope[1]}
    for rows in row_groups:
        for row in rows:
            start = max(scope[0], _start(row))
            end = min(scope[1], _end(row))
            if end > start:
                boundaries.update((start, end))
    ordered = sorted(boundaries)
    return [
        (ordered[index], ordered[index + 1])
        for index in range(len(ordered) - 1)
        if ordered[index + 1] > ordered[index]
    ]


def _source_refs(
    database: Database, session_id: int, start_ms: int, end_ms: int
) -> list[dict[str, Any]]:
    slices, gaps = resolve_session_slices(database, session_id, start_ms, end_ms)
    if gaps:
        raise RuntimeError(f"真值时间范围存在未映射的原始音频：{gaps}")
    return [
        {
            "source_object_id": item.source_object_id,
            "source_sha256": item.source_sha256,
            "source_start_ms": item.source_start_ms,
            "source_end_ms": item.source_end_ms,
        }
        for item in slices
    ]


def _annotation(
    key: str,
    kind: str,
    start_ms: int,
    end_ms: int,
    *,
    label: str | None = None,
    text: str | None = None,
    metadata: dict[str, Any] | None = None,
    legacy_segment_id: int | None = None,
    source_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "annotation_key": key,
        "annotation_kind": kind,
        "session_start_ms": start_ms,
        "session_end_ms": end_ms,
        "label": label,
        "text": text,
        "metadata": metadata or {},
        "legacy_segment_id": legacy_segment_id,
        "source_refs": source_refs or [],
    }


def _prediction(
    key: str,
    kind: str,
    start_ms: int,
    end_ms: int,
    *,
    label: str | None = None,
    text: str | None = None,
    confidence: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "prediction_key": key,
        "prediction_kind": kind,
        "session_start_ms": start_ms,
        "session_end_ms": end_ms,
        "label": label,
        "text": text,
        "confidence": confidence,
        "metadata": metadata or {},
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"第 {line_number} 行不是有效 JSON：{exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"第 {line_number} 行必须是 JSON 对象")
        rows.append(value)
    return rows


def _write_jsonl_atomically(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()
    temporary.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    if path.exists():
        temporary.unlink(missing_ok=True)
        raise FileExistsError(f"不会覆盖已有文件：{path}")
    temporary.replace(path)


def _canonical_refs(refs: Sequence[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return [
        (
            int(item["source_object_id"]),
            str(item["source_sha256"]),
            int(item["source_start_ms"]),
            int(item["source_end_ms"]),
        )
        for item in refs
    ]


def _of_kind(rows: Sequence[Any], kind: str) -> list[Any]:
    result: list[Any] = []
    for row in rows:
        value = _value(row, "annotation_kind")
        if value is None:
            value = _value(row, "prediction_kind")
        if value == kind:
            result.append(row)
    return result


def _value(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _start(row: Any) -> int:
    return int(_value(row, "session_start_ms"))


def _end(row: Any) -> int:
    return int(_value(row, "session_end_ms"))


def _interval(row: Any) -> tuple[int, int]:
    return _start(row), _end(row)


def _label(row: Any) -> str:
    return str(_value(row, "label") or "")


def _overlap_ms(row: Any, start_ms: int, end_ms: int) -> int:
    return max(0, min(_end(row), end_ms) - max(_start(row), start_ms))


def _has_active(rows: Sequence[Any], midpoint: float) -> bool:
    return any(_start(row) <= midpoint < _end(row) for row in rows)


def _active_labels(rows: Sequence[Any], midpoint: float) -> set[str]:
    return {
        _label(row)
        for row in rows
        if _label(row) and _start(row) <= midpoint < _end(row)
    }


def _entity_key(row: Any) -> tuple[str, str]:
    return _label(row), normalize_text(str(_value(row, "text") or ""))


def _metadata(row: Any) -> dict[str, Any]:
    value = _value(row, "metadata")
    if isinstance(value, dict):
        return value
    value = _value(row, "metadata_json")
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_ratio(numerator: int | float, denominator: int | float) -> float | None:
    return numerator / denominator if denominator else None


def _f1(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None:
        return None
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _mean(values: Sequence[int]) -> float | None:
    return sum(values) / len(values) if values else None


def _percentile(values: Sequence[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason}


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _annotation_metadata(row: dict[str, Any], key: str) -> dict[str, Any]:
    value = row.get("metadata", {})
    if not isinstance(value, dict):
        raise ValueError(f"标注 {key} 的 metadata 必须是对象")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _render_report(payload: dict[str, Any]) -> str:
    metrics = payload["metrics"]

    def display(value: Any) -> str:
        if value is None:
            return "N/A"
        if isinstance(value, float):
            return f"{value:.4f}"
        return str(value)

    return "\n".join(
        [
            "# 连续时间 Benchmark 报告",
            "",
            f"- Truth set：{payload['truth_set_id']}",
            f"- Prediction set：{payload['prediction_set_id']}",
            f"- 输入指纹：`{payload['input_fingerprint']}`",
            "",
            "| 指标 | 结果 |",
            "| --- | ---: |",
            f"| CER | {display(metrics['asr'].get('cer'))} |",
            f"| VAD Miss | {display(metrics['vad'].get('miss_rate'))} |",
            f"| VAD False Alarm | {display(metrics['vad'].get('false_alarm_rate'))} |",
            f"| DER | {display(metrics['speaker'].get('der'))} |",
            f"| JER | {display(metrics['speaker'].get('jer'))} |",
            "| 对齐平均边界误差 | "
            f"{display(metrics['alignment'].get('mean_absolute_boundary_error_ms'))} ms |",
            f"| 实体 F1 | {display(metrics['entities']['extraction'].get('f1'))} |",
            f"| 实体文字保留召回 | "
            f"{display(metrics['entities']['transcript_preservation'].get('recall'))} |",
            "",
            "N/A 表示真值覆盖或模型输出不足，系统没有用不完整标注伪造指标。",
            "",
        ]
    )
