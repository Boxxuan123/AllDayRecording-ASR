from __future__ import annotations

import hashlib
import json
import math
import random
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from allday_asr.asr.oracle_backends import OracleAsrBackend
from allday_asr.paths import EVALUATION_DIR, OUTPUT_DIR
from allday_asr.services.acoustic_selection import (
    analyze_pcm_wav,
    select_acoustic_windows,
)
from allday_asr.services.evaluation import (
    EVALUATION_FORMAT,
    NAME_PATTERN,
    levenshtein_operations,
    normalize_text,
)
from allday_asr.services.sources import (
    LogicalWindow,
    materialize_logical_window,
    resolve_session_slices,
    temporary_logical_window,
)
from allday_asr.storage.database import Database

CONTINUOUS_TRUTH_FORMAT = "AllDayRecording continuous truth v2"
BENCHMARK_REPORT_FORMAT = "AllDayRecording continuous benchmark report v1"
BLIND_PROTOCOL_FORMAT = "AllDayRecording V2-C.1 blind benchmark v1"
ACOUSTIC_BLIND_PROTOCOL_FORMAT = (
    "AllDayRecording V2-C.2 acoustic-stratified blind benchmark v1"
)
ACOUSTIC_BLIND_KIND = "blind_acoustic_stratified_annotation_v2"
COMPLETED_SUBSET_KIND = "human_limited_completed_blind_subset_v1"
COMPLETED_SUBSET_PROTOCOL_FORMAT = (
    "AllDayRecording V2-C.2 completed-window preliminary benchmark v1"
)
ACOUSTIC_SELECTION_MANIFEST_FORMAT = (
    "AllDayRecording V2-C.2 acoustic selection manifest v1"
)
SPEECH_SOURCE_POLICY_FORMAT = "AllDayRecording audible speech source v1"
SPEECH_SOURCE_LIVE = "live_person"
SPEECH_SOURCE_MEDIA = "media_playback"
SPEECH_SOURCE_MIXED = "mixed_live_media"
SPEECH_SOURCE_UNKNOWN = "unknown"
SPEECH_SOURCE_VALUES = frozenset(
    {
        SPEECH_SOURCE_LIVE,
        SPEECH_SOURCE_MEDIA,
        SPEECH_SOURCE_MIXED,
        SPEECH_SOURCE_UNKNOWN,
    }
)
# An older task without an explicit policy must not silently turn ambiguous
# playback speech into an in-person conversation. A task may override this when
# the annotator has explicitly classified all pre-existing rows.
LEGACY_SPEECH_SOURCE_DEFAULT = SPEECH_SOURCE_UNKNOWN
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


@dataclass(frozen=True)
class BlindTruthTaskSummary:
    session_id: int
    scope_start_ms: int
    scope_end_ms: int
    task_path: Path
    audio_paths: tuple[Path, ...]


def create_blind_truth_task(
    database: Database,
    session_id: int,
    *,
    name: str,
    duration_ms: int = 1_800_000,
    chunk_ms: int = 300_000,
    seed: str = "20260828",
    output_dir: Path | None = None,
) -> BlindTruthTaskSummary:
    """Create a model-independent continuous listening task from immutable audio."""
    if not NAME_PATTERN.fullmatch(name):
        raise ValueError("name 只能包含字母、数字、点、下划线和连字符，最长 64 字符")
    if duration_ms <= 0 or chunk_ms <= 0:
        raise ValueError("duration_ms 和 chunk_ms 必须大于 0")
    session = database.get_recording_session(session_id)
    session_duration = int(session["duration_ms"])
    if duration_ms > session_duration:
        raise ValueError("盲标时长不能超过录音会话时长")
    input_fingerprint = database.session_input_fingerprint(session_id)
    max_start_second = (session_duration - duration_ms) // 1_000
    selector = hashlib.sha256(
        f"{BLIND_PROTOCOL_FORMAT}:{input_fingerprint}:{seed}".encode("utf-8")
    ).digest()
    start_ms = (
        int.from_bytes(selector[:8], "big") % (max_start_second + 1)
    ) * 1_000
    end_ms = start_ms + duration_ms
    target = (
        output_dir.resolve()
        if output_dir is not None
        else EVALUATION_DIR / f"session-{session_id:06d}" / f"{name}-blind-v2c1"
    )
    if target.exists():
        raise FileExistsError(f"盲标任务已存在，不会覆盖：{target}")
    target.mkdir(parents=True)

    windows: list[dict[str, Any]] = []
    audio_paths: list[Path] = []
    cursor = start_ms
    position = 0
    while cursor < end_ms:
        window_end = min(end_ms, cursor + chunk_ms)
        slices, gaps = resolve_session_slices(database, session_id, cursor, window_end)
        window = LogicalWindow(
            session_id=session_id,
            index=position,
            core_start_ms=cursor,
            core_end_ms=window_end,
            analysis_start_ms=cursor,
            analysis_end_ms=window_end,
            slices=slices,
            uncovered_ranges=gaps,
        )
        audio_path = target / f"window-{position:03d}.wav"
        materialize_logical_window(window, audio_path)
        audio_paths.append(audio_path)
        windows.append(
            {
                "type": "blind_window",
                "window_index": position,
                "session_start_ms": cursor,
                "session_end_ms": window_end,
                "audio_file": audio_path.name,
                "audio_sha256": _sha256_file(audio_path),
                "review_status": "pending",
                "notes": "",
            }
        )
        cursor = window_end
        position += 1

    metadata = {
        "type": "metadata",
        "format": CONTINUOUS_TRUTH_FORMAT,
        "name": name,
        "session_id": session_id,
        "scope_start_ms": start_ms,
        "scope_end_ms": end_ms,
        "input_fingerprint": input_fingerprint,
        "completeness": {
            "vad": "pending",
            "transcript": "pending",
            "speaker": "none",
            "alignment": "none",
            "entities": "none",
        },
        "provenance": {
            "kind": "blind_continuous_annotation_v1",
            "protocol": BLIND_PROTOCOL_FORMAT,
            "speech_source_policy": SPEECH_SOURCE_POLICY_FORMAT,
            "legacy_unlabeled_speech_source": LEGACY_SPEECH_SOURCE_DEFAULT,
            "selection_algorithm": "sha256-uniform-contiguous-second-v1",
            "selection_seed": seed,
            "requested_duration_ms": duration_ms,
            "chunk_ms": chunk_ms,
            "source_selection_inputs": ["session duration", "input fingerprint", "seed"],
            "model_outputs_used_for_selection": False,
        },
        "blind_attestation": {
            "model_outputs_unseen": False,
            "annotator": "",
            "completed_at": None,
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    review_region = {
        "type": "annotation",
        "key": "blind:review-region:0000",
        "kind": "uncertain",
        "session_start_ms": start_ms,
        "session_end_ms": end_ms,
        "label": "review_region_complete_scope",
        "text": None,
        "metadata": {"protocol": BLIND_PROTOCOL_FORMAT},
    }
    task_path = target / "truth-draft.jsonl"
    _write_jsonl_atomically(task_path, [metadata, *windows, review_region])
    (target / "README.md").write_text(
        _blind_task_readme(task_path.name, windows), encoding="utf-8"
    )
    return BlindTruthTaskSummary(
        session_id=session_id,
        scope_start_ms=start_ms,
        scope_end_ms=end_ms,
        task_path=task_path,
        audio_paths=tuple(audio_paths),
    )


def create_acoustic_blind_truth_task(
    database: Database,
    session_id: int,
    *,
    name: str,
    review_duration_ms: int = 600_000,
    chunk_ms: int = 60_000,
    minimum_gap_ms: int = 60_000,
    seed: str = "v2c2-primary-20260828",
    output_dir: Path | None = None,
) -> BlindTruthTaskSummary:
    """Create a speech-enriched blind task using waveform-only acoustic ranks."""
    if not NAME_PATTERN.fullmatch(name):
        raise ValueError("name 只能包含字母、数字、点、下划线和连字符，最长 64 字符")
    if review_duration_ms <= 0 or chunk_ms <= 0:
        raise ValueError("review_duration_ms 和 chunk_ms 必须大于 0")
    if review_duration_ms % chunk_ms:
        raise ValueError("review_duration_ms 必须是 chunk_ms 的整数倍")
    if minimum_gap_ms < 0:
        raise ValueError("minimum_gap_ms 不能小于 0")
    session = database.get_recording_session(session_id)
    session_duration = int(session["duration_ms"])
    if chunk_ms > session_duration:
        raise ValueError("V2-C.2 单个候选窗口不能超过录音会话时长")
    window_count = review_duration_ms // chunk_ms
    if window_count * chunk_ms > session_duration:
        raise ValueError("V2-C.2 总复核时长不能超过录音会话时长")
    input_fingerprint = database.session_input_fingerprint(session_id)
    target = (
        output_dir.resolve()
        if output_dir is not None
        else EVALUATION_DIR / f"session-{session_id:06d}" / f"{name}-blind-v2c2"
    )
    if target.exists():
        raise FileExistsError(f"盲标任务已存在，不会覆盖：{target}")

    slices, gaps = resolve_session_slices(database, session_id, 0, session_duration)
    full_session = LogicalWindow(
        session_id=session_id,
        index=0,
        core_start_ms=0,
        core_end_ms=session_duration,
        analysis_start_ms=0,
        analysis_end_ms=session_duration,
        slices=slices,
        uncovered_ranges=gaps,
    )
    with temporary_logical_window(full_session) as audio_path:
        profile = analyze_pcm_wav(
            audio_path,
            candidate_window_ms=chunk_ms,
        )
    selection = select_acoustic_windows(
        profile,
        count=window_count,
        seed=seed,
        minimum_gap_ms=minimum_gap_ms,
    )

    target.mkdir(parents=True)
    manifest_path = target / "selection-manifest.json"
    manifest = {
        "format": ACOUSTIC_SELECTION_MANIFEST_FORMAT,
        "session_id": session_id,
        "input_fingerprint": input_fingerprint,
        "selection_seed": seed,
        "candidate_window_ms": chunk_ms,
        "requested_review_duration_ms": review_duration_ms,
        "requested_minimum_gap_ms": minimum_gap_ms,
        "effective_minimum_gap_ms": selection.effective_gap_ms,
        "frame_ms": profile.frame_ms,
        "sample_rate": profile.sample_rate,
        "global_noise_floor_dbfs": profile.global_noise_floor_dbfs,
        "activity_threshold_dbfs": profile.activity_threshold_dbfs,
        "score_weights": {
            "active_fraction": 0.50,
            "longest_active_run_fraction": 0.20,
            "p90_dbfs": 0.15,
            "rms_dbfs": 0.10,
            "voice_band_ratio": 0.05,
        },
        "candidate_asr_outputs_used": False,
        "candidates": [item.as_dict() for item in profile.candidates],
        "selected_starts_ms": [item.start_ms for item in selection.selected],
    }
    _write_json_atomically(manifest_path, manifest)

    windows: list[dict[str, Any]] = []
    review_regions: list[dict[str, Any]] = []
    audio_paths: list[Path] = []
    for position, selected in enumerate(selection.selected):
        window_slices, window_gaps = resolve_session_slices(
            database, session_id, selected.start_ms, selected.end_ms
        )
        window = LogicalWindow(
            session_id=session_id,
            index=position,
            core_start_ms=selected.start_ms,
            core_end_ms=selected.end_ms,
            analysis_start_ms=selected.start_ms,
            analysis_end_ms=selected.end_ms,
            slices=window_slices,
            uncovered_ranges=window_gaps,
        )
        audio_path = target / f"window-{position:03d}.wav"
        materialize_logical_window(window, audio_path)
        audio_paths.append(audio_path)
        windows.append(
            {
                "type": "blind_window",
                "window_index": position,
                "session_start_ms": selected.start_ms,
                "session_end_ms": selected.end_ms,
                "audio_file": audio_path.name,
                "audio_sha256": _sha256_file(audio_path),
                "review_status": "pending",
                "notes": "",
                "selection_track": "speech_enriched_acoustic",
                "selection_score": selected.score,
                "selection_rank": selected.score_rank,
                "acoustic_metrics": {
                    "rms_dbfs": selected.rms_dbfs,
                    "p90_dbfs": selected.p90_dbfs,
                    "active_fraction": selected.active_fraction,
                    "longest_active_run_fraction": (
                        selected.longest_active_run_fraction
                    ),
                    "voice_band_ratio": selected.voice_band_ratio,
                },
            }
        )
        review_regions.append(
            {
                "type": "annotation",
                "key": f"blind:review-region:{position:04d}",
                "kind": "uncertain",
                "session_start_ms": selected.start_ms,
                "session_end_ms": selected.end_ms,
                "label": "review_region_complete_scope",
                "text": None,
                "metadata": {
                    "protocol": ACOUSTIC_BLIND_PROTOCOL_FORMAT,
                    "window_index": position,
                },
            }
        )

    scope_start = min(item.start_ms for item in selection.selected)
    scope_end = max(item.end_ms for item in selection.selected)
    metadata = {
        "type": "metadata",
        "format": CONTINUOUS_TRUTH_FORMAT,
        "name": name,
        "session_id": session_id,
        "scope_start_ms": scope_start,
        "scope_end_ms": scope_end,
        "review_duration_ms": review_duration_ms,
        "coverage_semantics": "exhaustive_within_review_regions",
        "input_fingerprint": input_fingerprint,
        "completeness": {
            "vad": "pending",
            "transcript": "pending",
            "speaker": "none",
            "alignment": "none",
            "entities": "none",
        },
        "provenance": {
            "kind": ACOUSTIC_BLIND_KIND,
            "protocol": ACOUSTIC_BLIND_PROTOCOL_FORMAT,
            "speech_source_policy": SPEECH_SOURCE_POLICY_FORMAT,
            "legacy_unlabeled_speech_source": LEGACY_SPEECH_SOURCE_DEFAULT,
            "selection_algorithm": "waveform-acoustic-rank-with-gap-v1",
            "selection_seed": seed,
            "requested_review_duration_ms": review_duration_ms,
            "candidate_window_ms": chunk_ms,
            "requested_minimum_gap_ms": minimum_gap_ms,
            "effective_minimum_gap_ms": selection.effective_gap_ms,
            "source_selection_inputs": [
                "immutable PCM waveform",
                "session duration",
                "input fingerprint",
                "seed",
            ],
            "acoustic_features": [
                "100 ms RMS activity fraction",
                "longest active run",
                "RMS dBFS",
                "p90 frame dBFS",
                "200-4000 Hz energy ratio",
            ],
            "selection_manifest": manifest_path.name,
            "selection_manifest_sha256": _sha256_file(manifest_path),
            "model_outputs_used_for_selection": False,
            "candidate_asr_outputs_used": False,
        },
        "blind_attestation": {
            "model_outputs_unseen": False,
            "annotator": "",
            "completed_at": None,
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    task_path = target / "truth-draft.jsonl"
    _write_jsonl_atomically(task_path, [metadata, *windows, *review_regions])
    (target / "README.md").write_text(
        _blind_task_readme(task_path.name, windows, version="V2-C.2"),
        encoding="utf-8",
    )
    return BlindTruthTaskSummary(
        session_id=session_id,
        scope_start_ms=scope_start,
        scope_end_ms=scope_end,
        task_path=task_path,
        audio_paths=tuple(audio_paths),
    )


def freeze_completed_blind_subset(
    database: Database,
    task_path: Path,
    *,
    name: str,
    output_path: Path | None = None,
) -> ContinuousTruthSummary:
    """Freeze only human-completed V2-C.2 windows as an explicit preliminary set."""
    if not NAME_PATTERN.fullmatch(name):
        raise ValueError("预备真值名称无效")
    task_path = task_path.resolve(strict=True)
    rows = _read_jsonl(task_path)
    if not rows or rows[0].get("type") != "metadata":
        raise ValueError("V2-C.2 任务第一行必须是 metadata")
    source_metadata = rows[0]
    provenance = source_metadata.get("provenance", {})
    if not isinstance(provenance, dict) or provenance.get(
        "protocol"
    ) != ACOUSTIC_BLIND_PROTOCOL_FORMAT:
        raise ValueError("只支持从 V2-C.2 声学富集盲标任务派生预备真值")
    if provenance.get("model_outputs_used_for_selection") is not False or provenance.get(
        "candidate_asr_outputs_used"
    ) is not False:
        raise ValueError("源任务的窗口选择不是独立于模型输出的")

    windows = sorted(
        (deepcopy(row) for row in rows if row.get("type") == "blind_window"),
        key=lambda row: int(row["window_index"]),
    )
    if not windows:
        raise ValueError("源任务没有 blind_window")
    for expected_index, window in enumerate(windows):
        if int(window["window_index"]) != expected_index:
            raise ValueError("源任务 blind_window index 必须从 0 连续递增")
        audio_name = str(window.get("audio_file") or "")
        audio_path = task_path.parent / audio_name
        if Path(audio_name).name != audio_name or not audio_path.is_file():
            raise ValueError(f"源任务试听音频不存在：{audio_name}")
        if _sha256_file(audio_path) != str(window.get("audio_sha256") or ""):
            raise ValueError(f"源任务试听音频 SHA-256 不一致：{audio_name}")

    temporal_windows = [
        (int(window["session_start_ms"]), int(window["session_end_ms"]))
        for window in windows
    ]
    review_regions = [
        row
        for row in rows
        if row.get("type") == "annotation"
        and row.get("kind") == "uncertain"
        and row.get("label") == "review_region_complete_scope"
    ]
    review_intervals = sorted(
        (int(row["session_start_ms"]), int(row["session_end_ms"]))
        for row in review_regions
    )
    if review_intervals != sorted(temporal_windows):
        raise ValueError("源任务 review-region 与 blind_window 不一致")
    _validate_blind_speech_sources(rows, review_regions)

    manifest_name = str(provenance.get("selection_manifest") or "")
    if Path(manifest_name).name != manifest_name:
        raise ValueError("源任务 selection manifest 路径无效")
    manifest_path = task_path.parent / manifest_name
    if not manifest_path.is_file() or _sha256_file(manifest_path) != str(
        provenance.get("selection_manifest_sha256") or ""
    ):
        raise ValueError("源任务 selection manifest 缺失或 SHA-256 不一致")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get(
        "format"
    ) != ACOUSTIC_SELECTION_MANIFEST_FORMAT:
        raise ValueError("源任务 selection manifest 格式无效")
    if [int(value) for value in manifest.get("selected_starts_ms", [])] != [
        start for start, _ in temporal_windows
    ]:
        raise ValueError("源任务窗口与 selection manifest 不一致")

    completed_windows = [
        window for window in windows if window.get("review_status") == "complete"
    ]
    if not completed_windows:
        raise ValueError("源任务还没有任何已完整检查的窗口")
    completed_indexes = [int(window["window_index"]) for window in completed_windows]
    completed_intervals = [
        (int(window["session_start_ms"]), int(window["session_end_ms"]))
        for window in completed_windows
    ]
    included_review_regions = [
        deepcopy(row)
        for row in review_regions
        if (int(row["session_start_ms"]), int(row["session_end_ms"]))
        in completed_intervals
    ]

    legacy_source = str(
        provenance.get("legacy_unlabeled_speech_source")
        or LEGACY_SPEECH_SOURCE_DEFAULT
    )
    annotations: list[dict[str, Any]] = []
    for row in rows:
        if row.get("type") != "annotation" or row in review_regions:
            continue
        interval = (int(row["session_start_ms"]), int(row["session_end_ms"]))
        contained = any(
            start <= interval[0] < interval[1] <= end
            for start, end in completed_intervals
        )
        overlaps = any(
            interval[0] < end and interval[1] > start
            for start, end in completed_intervals
        )
        if overlaps and not contained:
            raise ValueError(f"标注 {row.get('key')} 跨越已完成窗口边界")
        if not contained:
            continue
        annotation = deepcopy(row)
        annotation_metadata = annotation.setdefault("metadata", {})
        if annotation_metadata.get("utterance_id") and not annotation_metadata.get(
            "speech_source"
        ):
            annotation_metadata["speech_source"] = legacy_source
            annotation_metadata["speech_source_inferred_from_task"] = True
        annotations.append(annotation)
    source_task_sha256 = _sha256_file(task_path)
    scope_start = min(start for start, _ in completed_intervals)
    scope_end = max(end for _, end in completed_intervals)
    derived_metadata = {
        "type": "metadata",
        "format": CONTINUOUS_TRUTH_FORMAT,
        "name": name,
        "session_id": int(source_metadata["session_id"]),
        "scope_start_ms": scope_start,
        "scope_end_ms": scope_end,
        "review_duration_ms": sum(end - start for start, end in completed_intervals),
        "coverage_semantics": "exhaustive_within_review_regions",
        "input_fingerprint": source_metadata["input_fingerprint"],
        "completeness": {
            "vad": EXHAUSTIVE,
            "transcript": EXHAUSTIVE,
            "speaker": "none",
            "alignment": "none",
            "entities": "none",
        },
        "provenance": {
            "kind": COMPLETED_SUBSET_KIND,
            "protocol": COMPLETED_SUBSET_PROTOCOL_FORMAT,
            "preliminary": True,
            "claim_limit": "completed windows only; not the full V2-C.2 holdout",
            "subset_rule": "all windows marked complete before any subset model evaluation",
            "completed_window_indices": completed_indexes,
            "excluded_window_indices": [
                int(window["window_index"])
                for window in windows
                if int(window["window_index"]) not in completed_indexes
            ],
            "source_task_path": str(task_path),
            "source_task_sha256": source_task_sha256,
            "source_protocol": provenance["protocol"],
            "source_selection_manifest": str(manifest_path.resolve()),
            "source_selection_manifest_sha256": _sha256_file(manifest_path),
            "model_outputs_used_for_selection": False,
            "candidate_asr_outputs_used": False,
            "speech_source_policy": SPEECH_SOURCE_POLICY_FORMAT,
            "legacy_unlabeled_speech_source": legacy_source,
            "completed_window_audio": [
                {
                    "window_index": int(window["window_index"]),
                    "session_start_ms": int(window["session_start_ms"]),
                    "session_end_ms": int(window["session_end_ms"]),
                    "audio_file": str(window["audio_file"]),
                    "audio_sha256": str(window["audio_sha256"]),
                }
                for window in completed_windows
            ],
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    target = (
        output_path.resolve()
        if output_path is not None
        else task_path.with_name(f"{name}.jsonl")
    )
    _write_jsonl_atomically(
        target,
        [derived_metadata, *included_review_regions, *annotations],
    )
    return import_continuous_truth(database, target)


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
    provenance = metadata.get("provenance", {})
    if not isinstance(provenance, dict):
        raise ValueError("provenance 必须是对象")
    if provenance.get("kind") in {
        "blind_continuous_annotation_v1",
        ACOUSTIC_BLIND_KIND,
    }:
        _validate_blind_truth(rows, metadata, scope_start, scope_end, truth_path)

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


def snapshot_oracle_asr_predictions(
    database: Database,
    truth_set_id: int,
    backend: OracleAsrBackend,
    *,
    name: str,
    language: str | None = "zh",
    on_progress: Callable[[int, int], None] | None = None,
) -> PredictionSnapshotSummary:
    """Run one ASR model on exactly the same human transcript intervals."""
    if not NAME_PATTERN.fullmatch(name):
        raise ValueError("预测快照名称无效")
    truth_set = database.get_truth_set(truth_set_id)
    session_id = int(truth_set["session_id"])
    input_fingerprint = database.session_input_fingerprint(session_id)
    if str(truth_set["input_fingerprint"]) != input_fingerprint:
        raise ValueError("冻结真值的输入指纹与当前录音会话不一致")
    references = database.list_truth_annotations(
        truth_set_id, annotation_kind="transcript"
    )
    if not references:
        raise ValueError("真值集没有 transcript 标注，无法运行同边界 ASR")

    predictions: list[dict[str, Any]] = []
    model_manifest: dict[str, Any] | None = None
    try:
        for index, reference in enumerate(references):
            start_ms = int(reference["session_start_ms"])
            end_ms = int(reference["session_end_ms"])
            slices, gaps = resolve_session_slices(
                database, session_id, start_ms, end_ms
            )
            window = LogicalWindow(
                session_id=session_id,
                index=index,
                core_start_ms=start_ms,
                core_end_ms=end_ms,
                analysis_start_ms=start_ms,
                analysis_end_ms=end_ms,
                slices=slices,
                uncovered_ranges=gaps,
            )
            with temporary_logical_window(window) as audio_path:
                result = backend.transcribe(audio_path, language=language)
            raw_canonical = json.dumps(
                result.raw_response,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            annotation_key = str(reference["annotation_key"])
            predictions.append(
                _prediction(
                    f"oracle:{index:06d}:{annotation_key}",
                    "transcript",
                    start_ms,
                    end_ms,
                    text=result.text,
                    metadata={
                        "track": "oracle-segmentation",
                        "truth_annotation_key": annotation_key,
                        "reference_boundary_used": True,
                        "reference_text_used": False,
                        "language": result.language,
                        "raw_response_sha256": hashlib.sha256(
                            raw_canonical.encode("utf-8")
                        ).hexdigest(),
                    },
                )
            )
            if on_progress is not None:
                on_progress(index + 1, len(references))
        model_manifest = {
            "model_id": backend.model_id,
            "model_revision": backend.model_revision,
            "backend": backend.backend_name,
            "parameters": backend.parameters(),
            "evaluation_track": "oracle-segmentation",
            "truth_set_id": truth_set_id,
            "reference_text_exposed_to_model": False,
        }
    finally:
        backend.close()

    canonical = json.dumps(
        predictions, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    content_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    row = database.create_benchmark_prediction_set(
        {
            "prediction_key": (
                f"oracle:{session_id}:{backend.model_id}:{name}:{content_sha256}"
            ),
            "name": name,
            "session_id": session_id,
            "input_fingerprint": input_fingerprint,
            "adapter": "oracle-asr-boundaries-v1",
            "model_manifest": model_manifest or {},
            "content_sha256": content_sha256,
        },
        predictions,
    )
    return PredictionSnapshotSummary(
        prediction_set_id=int(row["id"]),
        session_id=session_id,
        prediction_count=len(predictions),
        content_sha256=content_sha256,
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
    bounding_scope = (
        int(truth_set["scope_start_ms"]),
        int(truth_set["scope_end_ms"]),
    )
    evaluation_scopes = _evaluation_scopes(annotations, bounding_scope)
    annotations = [
        row for row in annotations if _overlap_scopes_ms(row, evaluation_scopes) > 0
    ]
    predictions = [
        row for row in predictions if _overlap_scopes_ms(row, evaluation_scopes) > 0
    ]
    transcript_exhaustive = completeness.get("transcript") == EXHAUSTIVE

    metrics = {
        "asr": _asr_metrics(
            annotations,
            predictions,
            normalizer=normalize_text,
            scope=evaluation_scopes,
            exhaustive=transcript_exhaustive,
        ),
        "asr_itn": _asr_metrics(
            annotations,
            predictions,
            normalizer=normalize_text_itn_equivalent,
            scope=evaluation_scopes,
            exhaustive=transcript_exhaustive,
        ),
        "vad": (
            _vad_metrics(annotations, predictions, evaluation_scopes)
            if completeness.get("vad") == EXHAUSTIVE
            else _unavailable(
                "truth coverage is not exhaustive; VAD misses and false alarms would be biased"
            )
        ),
        "speaker": (
            _speaker_metrics(annotations, predictions, evaluation_scopes)
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
        "evaluation_scope": "review_regions",
        "evaluation_regions": [
            {"start_ms": start, "end_ms": end}
            for start, end in evaluation_scopes
        ],
        "asr_normalizations": {
            "raw": "NFKC + lowercase + remove whitespace/punctuation",
            "itn_equivalent": (
                "raw + conservative canonical Chinese cardinal numerals to Arabic"
            ),
        },
    }
    details = {
        "truth_completeness": completeness,
        "truth_annotation_count": len(annotations),
        "prediction_count": len(predictions),
        "evaluated_duration_ms": sum(end - start for start, end in evaluation_scopes),
        "evaluation_region_count": len(evaluation_scopes),
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
                "asr_itn_cer": metrics.get("asr_itn", {}).get("cer"),
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


def paired_oracle_bootstrap(
    database: Database,
    truth_set_id: int,
    baseline_prediction_set_id: int,
    candidate_prediction_set_id: int,
    *,
    samples: int = 20_000,
    seed: int = 20_260_828,
    itn_equivalent: bool = False,
    speech_source: str | None = None,
) -> dict[str, Any]:
    """Paired bootstrap over human transcript intervals for oracle ASR snapshots."""
    if samples < 1:
        raise ValueError("samples 必须大于 0")
    if speech_source is not None and speech_source not in SPEECH_SOURCE_VALUES:
        raise ValueError(f"speech_source 无效：{speech_source}")
    truth = database.get_truth_set(truth_set_id)
    prediction_sets = [
        database.get_benchmark_prediction_set(baseline_prediction_set_id),
        database.get_benchmark_prediction_set(candidate_prediction_set_id),
    ]
    for prediction_set in prediction_sets:
        if int(prediction_set["session_id"]) != int(truth["session_id"]):
            raise ValueError("paired bootstrap 的真值和预测不属于同一会话")
        if str(prediction_set["adapter"]) != "oracle-asr-boundaries-v1":
            raise ValueError("paired bootstrap 只接受 oracle-asr-boundaries-v1 快照")
    references = database.list_truth_annotations(
        truth_set_id, annotation_kind="transcript"
    )
    if speech_source is not None:
        references = [
            reference
            for reference in references
            if str(
                _metadata(reference).get("speech_source")
                or LEGACY_SPEECH_SOURCE_DEFAULT
            )
            == speech_source
        ]
    if not references:
        raise ValueError("真值集没有 transcript 标注")
    normalizer = normalize_text_itn_equivalent if itn_equivalent else normalize_text

    def indexed_predictions(prediction_set_id: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for row in database.list_benchmark_predictions(
            prediction_set_id, prediction_kind="transcript"
        ):
            key = str(_metadata(row).get("truth_annotation_key") or "")
            if not key:
                raise ValueError("oracle ASR 快照缺少 truth_annotation_key")
            if key in result:
                raise ValueError(f"oracle ASR 快照包含重复真值键：{key}")
            result[key] = row
        return result

    baseline = indexed_predictions(baseline_prediction_set_id)
    candidate = indexed_predictions(candidate_prediction_set_id)
    items: list[tuple[int, int, int]] = []
    segment_wins = {"baseline": 0, "candidate": 0, "tie": 0}
    for reference in references:
        key = str(reference["annotation_key"])
        if key not in baseline or key not in candidate:
            raise ValueError(f"oracle ASR 快照没有完整覆盖真值键：{key}")
        normalized_reference = normalizer(str(reference["text"] or ""))
        if not normalized_reference:
            continue
        errors: list[int] = []
        for prediction in (baseline[key], candidate[key]):
            operations = levenshtein_operations(
                normalized_reference, normalizer(str(prediction["text"] or ""))
            )
            errors.append(
                operations["substitutions"]
                + operations["deletions"]
                + operations["insertions"]
            )
        items.append((len(normalized_reference), errors[0], errors[1]))
        if errors[0] < errors[1]:
            segment_wins["baseline"] += 1
        elif errors[1] < errors[0]:
            segment_wins["candidate"] += 1
        else:
            segment_wins["tie"] += 1
    if not items:
        raise ValueError("真值集没有非空 transcript 标注")

    reference_chars = sum(item[0] for item in items)
    baseline_errors = sum(item[1] for item in items)
    candidate_errors = sum(item[2] for item in items)
    rng = random.Random(seed)
    deltas: list[float] = []
    for _ in range(samples):
        drawn = [items[rng.randrange(len(items))] for _ in items]
        chars = sum(item[0] for item in drawn)
        baseline_cer = sum(item[1] for item in drawn) / chars
        candidate_cer = sum(item[2] for item in drawn) / chars
        deltas.append(candidate_cer - baseline_cer)
    ordered = sorted(deltas)
    return {
        "truth_set_id": truth_set_id,
        "baseline_prediction_set_id": baseline_prediction_set_id,
        "candidate_prediction_set_id": candidate_prediction_set_id,
        "normalization": "itn_equivalent" if itn_equivalent else "raw",
        "speech_source": speech_source,
        "evaluated_intervals": len(items),
        "reference_chars": reference_chars,
        "baseline": {
            "errors": baseline_errors,
            "cer": baseline_errors / reference_chars,
        },
        "candidate": {
            "errors": candidate_errors,
            "cer": candidate_errors / reference_chars,
        },
        "candidate_minus_baseline_cer": (
            candidate_errors - baseline_errors
        ) / reference_chars,
        "paired_bootstrap": {
            "samples": samples,
            "seed": seed,
            "confidence_interval_95": [
                ordered[max(0, math.ceil(0.025 * samples) - 1)],
                ordered[max(0, math.ceil(0.975 * samples) - 1)],
            ],
            "candidate_better_probability": sum(delta < 0 for delta in deltas) / samples,
            "tie_probability": sum(delta == 0 for delta in deltas) / samples,
        },
        "interval_wins": segment_wins,
    }


def _asr_metrics(
    annotations: Sequence[Any],
    predictions: Sequence[Any],
    *,
    normalizer: Callable[[str], str],
    scope: tuple[int, int] | Sequence[tuple[int, int]],
    exhaustive: bool,
) -> dict[str, Any]:
    scopes = _normalize_scopes(scope)
    references = [
        row
        for row in _of_kind(annotations, "transcript")
        if _overlap_scopes_ms(row, scopes) > 0
    ]
    hypotheses = [
        row
        for row in _of_kind(predictions, "transcript")
        if _overlap_scopes_ms(row, scopes) > 0
    ]
    boundary_crossing = [
        row for row in hypotheses if not _contained_in_scopes(row, scopes)
    ]
    if len(scopes) > 1 and boundary_crossing:
        raise ValueError(
            "预测 transcript 跨越非连续 review-region 边界；"
            "必须按 review-region 重跑或使用 token 时间裁剪快照"
        )
    excluded_ranges = [
        row
        for row in _of_kind(annotations, "uncertain")
        if _label(row) in {"unintelligible", "exclude_asr"}
    ]
    if excluded_ranges:
        references = [
            row
            for row in references
            if not any(
                _overlap_ms(row, _start(excluded), _end(excluded)) > 0
                for excluded in excluded_ranges
            )
        ]
        hypotheses = [
            row
            for row in hypotheses
            if not any(
                _overlap_ms(row, _start(excluded), _end(excluded)) > 0
                for excluded in excluded_ranges
            )
        ]
    totals = {"reference_chars": 0, "substitutions": 0, "deletions": 0, "insertions": 0}
    exact = 0
    details: list[dict[str, Any]] = []
    components = _transcript_components(references, hypotheses)
    connected_hypotheses = {
        id(hypothesis)
        for _, component_hypotheses in components
        for hypothesis in component_hypotheses
    }
    orphan_hypotheses = [
        hypothesis
        for hypothesis in hypotheses
        if id(hypothesis) not in connected_hypotheses
    ]
    if exhaustive and orphan_hypotheses:
        components.append(([], orphan_hypotheses))
    for component_references, component_hypotheses in components:
        component_references.sort(key=lambda row: (_start(row), _end(row)))
        component_hypotheses.sort(key=lambda row: (_start(row), _end(row)))
        reference_text = "".join(
            str(_value(row, "text") or "") for row in component_references
        )
        hypothesis_text = "".join(
            str(_value(row, "text") or "") for row in component_hypotheses
        )
        normalized_reference = normalizer(reference_text)
        normalized_hypothesis = normalizer(hypothesis_text)
        operations = levenshtein_operations(
            normalized_reference, normalized_hypothesis
        )
        totals["reference_chars"] += len(normalized_reference)
        for key in ("substitutions", "deletions", "insertions"):
            totals[key] += operations[key]
        exact += normalized_reference == normalized_hypothesis
        component_rows = [*component_references, *component_hypotheses]
        details.append(
            {
                "annotation_keys": [
                    _value(row, "annotation_key") for row in component_references
                ],
                "start_ms": min(_start(row) for row in component_rows),
                "end_ms": max(_end(row) for row in component_rows),
                "reference_text": reference_text,
                "hypothesis_text": hypothesis_text,
                "operations": operations,
                "orphan_hypothesis_component": not component_references,
            }
        )
    errors = totals["substitutions"] + totals["deletions"] + totals["insertions"]
    return {
        "available": bool(references) or exhaustive,
        "evaluated_intervals": len(references),
        "evaluation_components": len(components),
        "exhaustive": exhaustive,
        "excluded_uncertain_ranges": len(excluded_ranges),
        "orphan_hypotheses": len(orphan_hypotheses) if exhaustive else 0,
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
    annotations: Sequence[Any],
    predictions: Sequence[Any],
    scope: tuple[int, int] | Sequence[tuple[int, int]],
) -> dict[str, Any]:
    scopes = _normalize_scopes(scope)
    references = [
        row
        for row in _of_kind(annotations, "speech")
        if _overlap_scopes_ms(row, scopes) > 0
    ]
    hypotheses = [
        row
        for row in _of_kind(predictions, "speech")
        if _overlap_scopes_ms(row, scopes) > 0
    ]
    atoms = [
        atom
        for evaluation_scope in scopes
        for atom in _time_atoms(evaluation_scope, references, hypotheses)
    ]
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
        {
            boundary
            for row in references
            for start, end in scopes
            if _overlap_ms(row, start, end) > 0
            for boundary in (max(start, _start(row)), min(end, _end(row)))
        }
    )
    hyp_boundaries = sorted(
        {
            boundary
            for row in hypotheses
            for start, end in scopes
            if _overlap_ms(row, start, end) > 0
            for boundary in (max(start, _start(row)), min(end, _end(row)))
        }
    )
    boundary_errors = [
        min(abs(boundary - candidate) for candidate in hyp_boundaries)
        for boundary in ref_boundaries
        if hyp_boundaries
    ]
    return {
        "available": True,
        "scope_ms": sum(end - start for start, end in scopes),
        "scope_regions": len(scopes),
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
    annotations: Sequence[Any],
    predictions: Sequence[Any],
    scope: tuple[int, int] | Sequence[tuple[int, int]],
) -> dict[str, Any]:
    scopes = _normalize_scopes(scope)
    references = [
        row
        for row in _of_kind(annotations, "speaker")
        if _label(row) and _overlap_scopes_ms(row, scopes) > 0
    ]
    hypotheses = [
        row
        for row in _of_kind(predictions, "speaker")
        if _label(row) and _overlap_scopes_ms(row, scopes) > 0
    ]
    atoms = [
        atom
        for evaluation_scope in scopes
        for atom in _time_atoms(evaluation_scope, references, hypotheses)
    ]
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


def _evaluation_scopes(
    annotations: Sequence[Any], fallback: tuple[int, int]
) -> list[tuple[int, int]]:
    review_regions = [
        (_start(row), _end(row))
        for row in _of_kind(annotations, "uncertain")
        if _label(row) == "review_region_complete_scope"
    ]
    return _normalize_scopes(review_regions or fallback)


def _normalize_scopes(
    scope: tuple[int, int] | Sequence[tuple[int, int]],
) -> list[tuple[int, int]]:
    if (
        isinstance(scope, tuple)
        and len(scope) == 2
        and isinstance(scope[0], int)
        and isinstance(scope[1], int)
    ):
        scopes = [scope]
    else:
        scopes = [(int(item[0]), int(item[1])) for item in scope]
    if not scopes:
        raise ValueError("评测范围不能为空")
    scopes.sort()
    previous_end: int | None = None
    for start, end in scopes:
        if start < 0 or end <= start:
            raise ValueError("评测范围无效")
        if previous_end is not None and start < previous_end:
            raise ValueError("评测范围不能重叠")
        previous_end = end
    return scopes


def _overlap_scopes_ms(row: Any, scopes: Sequence[tuple[int, int]]) -> int:
    return sum(_overlap_ms(row, start, end) for start, end in scopes)


def _contained_in_scopes(row: Any, scopes: Sequence[tuple[int, int]]) -> bool:
    return any(
        start <= _start(row) < _end(row) <= end
        for start, end in scopes
    )


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


def normalize_text_itn_equivalent(value: str) -> str:
    """Conservative CER view that only folds unambiguous Chinese cardinals."""
    normalized = normalize_text(value)

    def normalize_token(raw_token: str) -> str:
        token = raw_token.replace("两", "二").replace("〇", "零")
        digit_characters = set("零一二三四五六七八九")
        for split_at in range(len(token), 1, -1):
            prefix = token[:split_at]
            if not any(item in digit_characters for item in prefix):
                continue
            try:
                parsed = _parse_chinese_cardinal(prefix)
                canonical = _format_chinese_cardinal(parsed)
            except ValueError:
                continue
            if canonical == prefix:
                suffix = token[split_at:]
                return str(parsed) + (normalize_token(suffix) if suffix else "")
        return token

    return re.sub(
        r"[零〇一二两三四五六七八九十百千万亿]+",
        lambda match: normalize_token(match.group(0)),
        normalized,
    )


def _parse_chinese_cardinal(token: str) -> int:
    digits = {
        "零": 0,
        "一": 1,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    small_units = {"十": 10, "百": 100, "千": 1_000}
    big_units = {"万": 10_000, "亿": 100_000_000}
    if not any(item in small_units or item in big_units for item in token):
        raise ValueError("not a cardinal with a multiplier")
    total = section = number = 0
    for character in token:
        if character in digits:
            number = digits[character]
        elif character in small_units:
            unit = small_units[character]
            section += (number or 1) * unit
            number = 0
        elif character in big_units:
            section += number
            total += (section or 1) * big_units[character]
            section = number = 0
        else:
            raise ValueError("unsupported Chinese numeral")
    return total + section + number


def _format_chinese_cardinal(value: int) -> str:
    if value < 0 or value >= 100_000_000:
        raise ValueError("Chinese cardinal is outside the conservative range")
    digits = "零一二三四五六七八九"

    def group(number: int) -> str:
        if number == 0:
            return "零"
        result: list[str] = []
        pending_zero = False
        for divisor, unit in ((1_000, "千"), (100, "百"), (10, "十"), (1, "")):
            digit = number // divisor
            number %= divisor
            if digit:
                if pending_zero and result:
                    result.append("零")
                result.extend((digits[digit], unit))
                pending_zero = False
            elif result and number:
                pending_zero = True
        text = "".join(result)
        return text[1:] if text.startswith("一十") else text

    if value < 10_000:
        return group(value)
    high, low = divmod(value, 10_000)
    text = group(high) + "万"
    if low == 0:
        return text
    if low < 1_000:
        text += "零"
    return text + group(low)


def _validate_blind_truth(
    rows: Sequence[dict[str, Any]],
    metadata: dict[str, Any],
    scope_start: int,
    scope_end: int,
    truth_path: Path,
) -> None:
    provenance = metadata.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("盲标真值缺少协议声明")
    protocol = provenance.get("protocol")
    if protocol not in {BLIND_PROTOCOL_FORMAT, ACOUSTIC_BLIND_PROTOCOL_FORMAT}:
        raise ValueError("盲标真值缺少受支持的 V2-C 协议声明")
    acoustic_stratified = protocol == ACOUSTIC_BLIND_PROTOCOL_FORMAT
    if provenance.get("model_outputs_used_for_selection") is not False:
        raise ValueError("盲标范围必须在不使用模型输出的情况下选择")
    if acoustic_stratified and provenance.get("candidate_asr_outputs_used") is not False:
        raise ValueError("V2-C.2 选择过程不得使用候选 ASR 输出")
    attestation = metadata.get("blind_attestation")
    if not isinstance(attestation, dict):
        raise ValueError("盲标真值缺少 blind_attestation")
    if attestation.get("model_outputs_unseen") is not True:
        raise ValueError("盲标者必须确认 model_outputs_unseen=true")
    if not str(attestation.get("annotator") or "").strip():
        raise ValueError("盲标者必须填写 annotator")
    if not str(attestation.get("completed_at") or "").strip():
        raise ValueError("盲标者必须填写 completed_at")
    completeness = metadata.get("completeness", {})
    if completeness.get("vad") != EXHAUSTIVE:
        raise ValueError("盲标真值只有在 vad=exhaustive 后才能冻结")
    if completeness.get("transcript") != EXHAUSTIVE:
        raise ValueError("盲标真值只有在 transcript=exhaustive 后才能冻结")

    forbidden_keys = {
        "hypothesis_text",
        "hypothesis_text_at_export",
        "prediction_text",
        "model_output",
        "asr_output",
        "segment_id",
        "legacy_segment_id",
    }

    def inspect(value: Any) -> None:
        if isinstance(value, dict):
            present = forbidden_keys & set(value)
            if present:
                raise ValueError(f"盲标文件包含被禁止的模型/V1 字段：{sorted(present)}")
            for nested in value.values():
                inspect(nested)
        elif isinstance(value, list):
            for nested in value:
                inspect(nested)

    inspect(rows)
    allowed_types = {"metadata", "blind_window", "annotation"}
    unsupported = [row.get("type") for row in rows if row.get("type") not in allowed_types]
    if unsupported:
        raise ValueError(f"盲标文件包含不支持的行类型：{unsupported}")
    windows = sorted(
        (row for row in rows if row.get("type") == "blind_window"),
        key=lambda row: int(row["window_index"]),
    )
    if not windows:
        raise ValueError("盲标真值没有 blind_window")
    temporal_windows: list[tuple[int, int]] = []
    for expected_index, window in enumerate(windows):
        if int(window["window_index"]) != expected_index:
            raise ValueError("blind_window index 必须从 0 连续递增")
        start_ms = int(window["session_start_ms"])
        end_ms = int(window["session_end_ms"])
        if start_ms < scope_start or end_ms <= start_ms or end_ms > scope_end:
            raise ValueError("blind_window 超出真值包围范围")
        temporal_windows.append((start_ms, end_ms))
        if window.get("review_status") != "complete":
            raise ValueError(f"blind_window {expected_index} 尚未标记 complete")
        audio_name = str(window.get("audio_file") or "")
        if Path(audio_name).name != audio_name:
            raise ValueError("blind_window audio_file 必须是同目录文件名")
        audio_path = truth_path.parent / audio_name
        if not audio_path.is_file():
            raise ValueError(f"盲标派生音频不存在：{audio_path}")
        if _sha256_file(audio_path) != str(window.get("audio_sha256") or ""):
            raise ValueError(f"盲标派生音频 SHA-256 不一致：{audio_path}")
    temporal_windows.sort()
    if temporal_windows[0][0] != scope_start or temporal_windows[-1][1] != scope_end:
        raise ValueError("blind_window 与真值包围范围不一致")
    for previous, current in zip(temporal_windows, temporal_windows[1:]):
        if current[0] < previous[1]:
            raise ValueError("blind_window 不能重叠")
        if not acoustic_stratified and current[0] != previous[1]:
            raise ValueError("V2-C.1 blind_window 必须无缝覆盖完整真值范围")
    if not acoustic_stratified and sum(
        end - start for start, end in temporal_windows
    ) != scope_end - scope_start:
        raise ValueError("V2-C.1 blind_window 没有覆盖完整真值范围")

    if acoustic_stratified:
        manifest_name = str(provenance.get("selection_manifest") or "")
        if Path(manifest_name).name != manifest_name:
            raise ValueError("V2-C.2 selection manifest 路径无效")
        manifest_path = truth_path.parent / manifest_name
        if not manifest_path.is_file():
            raise ValueError("V2-C.2 selection manifest 不存在")
        if _sha256_file(manifest_path) != str(
            provenance.get("selection_manifest_sha256") or ""
        ):
            raise ValueError("V2-C.2 selection manifest SHA-256 不一致")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("V2-C.2 selection manifest 不是有效 JSON") from exc
        if not isinstance(manifest, dict) or manifest.get(
            "format"
        ) != ACOUSTIC_SELECTION_MANIFEST_FORMAT:
            raise ValueError("V2-C.2 selection manifest 格式无效")
        if int(manifest.get("session_id", -1)) != int(metadata["session_id"]):
            raise ValueError("V2-C.2 selection manifest 会话不一致")
        if manifest.get("input_fingerprint") != metadata.get("input_fingerprint"):
            raise ValueError("V2-C.2 selection manifest 原音指纹不一致")
        if manifest.get("candidate_asr_outputs_used") is not False:
            raise ValueError("V2-C.2 selection manifest 使用了候选 ASR 输出")
        selected_starts = [int(value) for value in manifest.get("selected_starts_ms", [])]
        if selected_starts != [start for start, _ in temporal_windows]:
            raise ValueError("V2-C.2 窗口不是 selection manifest 预先选定的范围")
        manifest_candidates = {
            int(item["start_ms"]): item
            for item in manifest.get("candidates", [])
            if isinstance(item, dict) and "start_ms" in item
        }
        for window in windows:
            start_ms = int(window["session_start_ms"])
            candidate = manifest_candidates.get(start_ms)
            if candidate is None or int(candidate.get("end_ms", -1)) != int(
                window["session_end_ms"]
            ):
                raise ValueError("V2-C.2 窗口不在 selection manifest 候选中")
            if float(candidate.get("score", -1)) != float(
                window.get("selection_score", -2)
            ) or int(candidate.get("score_rank", -1)) != int(
                window.get("selection_rank", -2)
            ):
                raise ValueError("V2-C.2 窗口声学排名与 selection manifest 不一致")
    review_regions = [
        row
        for row in rows
        if row.get("type") == "annotation"
        and row.get("kind") == "uncertain"
        and row.get("label") == "review_region_complete_scope"
    ]
    review_intervals = sorted(
        (
            int(row["session_start_ms"]),
            int(row["session_end_ms"]),
        )
        for row in review_regions
    )
    if acoustic_stratified:
        if review_intervals != temporal_windows:
            raise ValueError("V2-C.2 必须为每个窗口保留完全一致的 review-region 行")
    elif review_intervals != [(scope_start, scope_end)]:
        raise ValueError("V2-C.1 必须保留覆盖完整 scope 的 review-region 行")
    for row in rows:
        if row.get("type") != "annotation" or row in review_regions:
            continue
        start_ms = int(row["session_start_ms"])
        end_ms = int(row["session_end_ms"])
        if acoustic_stratified and not any(
            window_start <= start_ms < end_ms <= window_end
            for window_start, window_end in temporal_windows
        ):
            raise ValueError(f"盲标标注 {row.get('key')} 不在任何已复核窗口内")
    _validate_blind_speech_sources(rows, review_regions)


def _validate_blind_speech_sources(
    rows: Sequence[dict[str, Any]],
    review_regions: Sequence[dict[str, Any]],
) -> None:
    grouped_sources: dict[str, set[str]] = {}
    review_region_ids = {id(row) for row in review_regions}
    metadata = rows[0] if rows else {}
    provenance = metadata.get("provenance", {})
    legacy_default = (
        str(provenance.get("legacy_unlabeled_speech_source"))
        if isinstance(provenance, dict)
        and provenance.get("legacy_unlabeled_speech_source") is not None
        else LEGACY_SPEECH_SOURCE_DEFAULT
    )
    if legacy_default not in SPEECH_SOURCE_VALUES:
        raise ValueError(
            "盲标协议的 legacy_unlabeled_speech_source 无效："
            f"{legacy_default}"
        )
    for row in rows:
        if row.get("type") != "annotation" or id(row) in review_region_ids:
            continue
        row_metadata = row.get("metadata", {})
        if not isinstance(row_metadata, dict):
            raise ValueError(f"标注 {row.get('key')} 的 metadata 必须是对象")
        explicit_source = row_metadata.get("speech_source")
        speech_source = str(explicit_source or legacy_default)
        if speech_source not in SPEECH_SOURCE_VALUES:
            raise ValueError(
                f"盲标标注 {row.get('key')} 的 speech_source 无效：{speech_source}"
            )
        utterance_id = str(row_metadata.get("utterance_id") or "").strip()
        if utterance_id:
            grouped_sources.setdefault(utterance_id, set()).add(speech_source)
    inconsistent = sorted(
        utterance_id
        for utterance_id, sources in grouped_sources.items()
        if len(sources) != 1
    )
    if inconsistent:
        raise ValueError(f"以下语音的 speech_source 不一致：{inconsistent}")


def _blind_task_readme(
    task_name: str,
    windows: Sequence[dict[str, Any]],
    *,
    version: str = "V2-C.1",
) -> str:
    window_lines = "\n".join(
        f"- `{row['audio_file']}`：{row['session_start_ms']}–{row['session_end_ms']} ms"
        for row in windows
    )
    return f"""# {version} 盲标任务

只听本目录 WAV，不打开网页中的 V1/V2 转写。原始 Watch 文件没有被修改；这些 WAV 是可重建的 16 kHz 单声道派生片段。

{window_lines}

在 `{task_name}` 中完成以下工作：

1. 每段完整听完，把对应 `blind_window.review_status` 改为 `complete`。
2. 为每段可听语音（包括电视/媒体播放声）添加 `speech` annotation，并为所有可听清内容添加 `transcript`；metadata 的 `speech_source` 使用 `live_person`、`media_playback`、`mixed_live_media` 或 `unknown`。无法可靠听写的语音另加 `uncertain` annotation，label 使用 `unintelligible`，该范围不计 ASR CER。时间是 session 绝对毫秒；key 必须唯一。
3. 现场声和媒体声重叠且难以拆分时直接使用 `mixed_live_media`，不必强行分离；听不清时同时使用 `unintelligible`。无语音区域不添加 `speech`，这正是穷尽式 VAD 真值的一部分。不要删除 `blind:review-region:*` 行。穷尽真值中的孤立模型 transcript 会作为插入错误计入，不能靠不重叠人工文字逃避处罚。
4. 全部完成后把 `completeness.vad` 和 `completeness.transcript` 改为 `exhaustive`，填写 `blind_attestation`，再运行 `allday-asr benchmark import-truth <path>`。

示例 annotation（仅展示格式，不是答案）：

```json
{{"type":"annotation","key":"blind:speech:0001","kind":"speech","session_start_ms":123000,"session_end_ms":124500,"label":"speech","text":null,"metadata":{{"reviewed":true,"speech_source":"live_person"}}}}
{{"type":"annotation","key":"blind:transcript:0001","kind":"transcript","session_start_ms":123000,"session_end_ms":124500,"label":null,"text":"人工听写内容","metadata":{{"reviewed":true,"speech_source":"live_person"}}}}
```
"""


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


def _write_json_atomically(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
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
            f"| 原始规范化 CER | {display(metrics['asr'].get('cer'))} |",
            f"| ITN 等价 CER | {display(metrics['asr_itn'].get('cer'))} |",
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
