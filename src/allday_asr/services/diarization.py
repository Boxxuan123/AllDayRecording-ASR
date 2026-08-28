from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from allday_asr.asr.funasr_backend import SPEAKER_MODEL_ID, FunASRBackend
from allday_asr.paths import recording_output_dir
from allday_asr.storage.database import Database


@dataclass(frozen=True)
class DiarizationSummary:
    recording_id: int
    assigned_segments: int
    unassigned_segments: int
    speakers: list[str]
    centers_path: Path | None
    rejected_short: int = 0
    rejected_weak_cluster: int = 0


@dataclass(frozen=True)
class SpeakerAuditSummary:
    recording_id: int
    kept_segments: int
    rejected_short: int
    rejected_weak_cluster: int
    speakers: list[str]


def diarize_recording(
    database: Database,
    recording_id: int,
    *,
    device: str = "auto",
    preset_speakers: int | None = None,
    min_segment_ms: int = 1_500,
    min_cluster_segments: int = 3,
    min_cluster_speech_ms: int = 5_000,
) -> DiarizationSummary:
    recording = database.get_recording(recording_id)
    normalized_value = recording["normalized_path"]
    if not normalized_value or not Path(normalized_value).is_file():
        raise RuntimeError("尚无标准化音频，请先执行 process")
    segments = database.all_segments(recording_id)
    if not segments:
        raise RuntimeError("尚无 VAD 片段，请先执行 process")

    backend = FunASRBackend(device=device)
    database.set_stage(
        recording_id,
        "diarization",
        "running",
        model_id=SPEAKER_MODEL_ID,
        model_version=backend.package_version,
    )
    try:
        result = backend.diarize(Path(normalized_value), preset_speakers=preset_speakers)
        raw_assignments = match_speaker_turns(segments, result.turns)
        assignments, filter_stats = filter_speaker_assignments(
            segments,
            raw_assignments,
            min_segment_ms=min_segment_ms,
            min_cluster_segments=min_cluster_segments,
            min_cluster_speech_ms=min_cluster_speech_ms,
        )
        database.replace_speaker_labels(recording_id, assignments)

        centers_path: Path | None = None
        if result.speaker_centers is not None and result.speaker_centers.size:
            centers_path = recording_output_dir(recording_id) / "speaker-centers.npz"
            np.savez_compressed(centers_path, centers=result.speaker_centers)

        labels = sorted({label for _, label in assignments if label is not None})
        assigned = sum(label is not None for _, label in assignments)
        summary = DiarizationSummary(
            recording_id=recording_id,
            assigned_segments=assigned,
            unassigned_segments=len(assignments) - assigned,
            speakers=labels,
            centers_path=centers_path,
            rejected_short=filter_stats["rejected_short"],
            rejected_weak_cluster=filter_stats["rejected_weak_cluster"],
        )
        database.set_stage(
            recording_id,
            "diarization",
            "completed",
            model_id=SPEAKER_MODEL_ID,
            model_version=backend.package_version,
            details={
                "assigned_segments": summary.assigned_segments,
                "unassigned_segments": summary.unassigned_segments,
                "speakers": summary.speakers,
                "centers_path": str(centers_path) if centers_path else None,
                "preset_speakers": preset_speakers,
                "min_segment_ms": min_segment_ms,
                "min_cluster_segments": min_cluster_segments,
                "min_cluster_speech_ms": min_cluster_speech_ms,
                **filter_stats,
            },
        )
        return summary
    except Exception as exc:
        database.set_stage(
            recording_id,
            "diarization",
            "failed",
            model_id=SPEAKER_MODEL_ID,
            model_version=backend.package_version,
            error=repr(exc),
        )
        raise


def match_speaker_turns(segments, turns: list[dict]) -> list[tuple[int, str | None]]:
    """Match diarization turns to persisted VAD segments by temporal overlap."""
    normalized_turns: list[tuple[int, int, str]] = []
    for turn in turns:
        try:
            start_ms = int(round(float(turn.get("start", 0))))
            end_ms = int(round(float(turn.get("end", 0))))
        except (TypeError, ValueError):
            continue
        if end_ms <= start_ms or turn.get("spk") is None:
            continue
        normalized_turns.append((start_ms, end_ms, _speaker_label(turn["spk"])))

    assignments: list[tuple[int, str | None]] = []
    for segment in segments:
        start_ms = int(segment["start_ms"])
        end_ms = int(segment["end_ms"])
        duration = max(1, end_ms - start_ms)
        best_label: str | None = None
        best_score = 0.0
        for turn_start, turn_end, label in normalized_turns:
            if turn_end <= start_ms:
                continue
            if turn_start >= end_ms:
                break
            overlap = max(0, min(end_ms, turn_end) - max(start_ms, turn_start))
            score = overlap / duration
            if score > best_score:
                best_score = score
                best_label = label
        assignments.append((int(segment["id"]), best_label if best_score >= 0.3 else None))
    return assignments


def filter_speaker_assignments(
    segments,
    assignments: list[tuple[int, str | None]],
    *,
    min_segment_ms: int = 1_500,
    min_cluster_segments: int = 3,
    min_cluster_speech_ms: int = 5_000,
) -> tuple[list[tuple[int, str | None]], dict[str, int]]:
    """Reject embeddings that are too short and clusters without enough evidence."""
    if min_segment_ms < 0 or min_cluster_segments < 1 or min_cluster_speech_ms < 0:
        raise ValueError("说话人质量阈值无效")
    segment_by_id = {int(segment["id"]): segment for segment in segments}
    evidence: dict[str, list[int]] = {}
    for segment_id, label in assignments:
        segment = segment_by_id.get(segment_id)
        if segment is None or label is None:
            continue
        duration = int(segment["end_ms"]) - int(segment["start_ms"])
        if duration >= min_segment_ms:
            evidence.setdefault(label, []).append(duration)

    reliable_labels = {
        label
        for label, durations in evidence.items()
        if len(durations) >= min_cluster_segments
        and sum(durations) >= min_cluster_speech_ms
    }
    filtered: list[tuple[int, str | None]] = []
    rejected_short = 0
    rejected_weak_cluster = 0
    for segment_id, label in assignments:
        segment = segment_by_id.get(segment_id)
        if segment is None or label is None:
            filtered.append((segment_id, None))
            continue
        duration = int(segment["end_ms"]) - int(segment["start_ms"])
        if duration < min_segment_ms:
            rejected_short += 1
            filtered.append((segment_id, None))
        elif label not in reliable_labels:
            rejected_weak_cluster += 1
            filtered.append((segment_id, None))
        else:
            filtered.append((segment_id, label))
    return filtered, {
        "rejected_short": rejected_short,
        "rejected_weak_cluster": rejected_weak_cluster,
    }


def audit_speaker_assignments(
    database: Database,
    recording_id: int,
    *,
    min_segment_ms: int = 1_500,
    min_cluster_segments: int = 3,
    min_cluster_speech_ms: int = 5_000,
) -> SpeakerAuditSummary:
    """Apply the conservative quality gate to existing derived speaker labels."""
    database.get_recording(recording_id)
    segments = database.all_segments(recording_id)
    if not segments:
        raise RuntimeError("当前录音没有语音片段")
    if any(segment["person_id"] is not None for segment in segments):
        raise RuntimeError("存在已确认身份，请先撤销身份标记再执行质量审计")
    assignments = [
        (int(segment["id"]), segment["speaker_session_id"]) for segment in segments
    ]
    if not any(label for _, label in assignments):
        raise RuntimeError("当前录音没有说话人标签，请先执行 diarize")
    filtered, stats = filter_speaker_assignments(
        segments,
        assignments,
        min_segment_ms=min_segment_ms,
        min_cluster_segments=min_cluster_segments,
        min_cluster_speech_ms=min_cluster_speech_ms,
    )
    database.replace_speaker_labels(recording_id, filtered)
    labels = sorted({label for _, label in filtered if label is not None})
    kept = sum(label is not None for _, label in filtered)
    refresh_diarization_stage_metadata(
        database,
        recording_id,
        details_update={
            "min_segment_ms": min_segment_ms,
            "min_cluster_segments": min_cluster_segments,
            "min_cluster_speech_ms": min_cluster_speech_ms,
            "quality_audit_applied": True,
            **stats,
        },
    )
    return SpeakerAuditSummary(
        recording_id=recording_id,
        kept_segments=kept,
        rejected_short=stats["rejected_short"],
        rejected_weak_cluster=stats["rejected_weak_cluster"],
        speakers=labels,
    )


def refresh_diarization_stage_metadata(
    database: Database,
    recording_id: int,
    *,
    details_update: dict | None = None,
) -> dict:
    """Synchronize the stage summary with the speaker labels currently in SQLite."""
    segments = database.all_segments(recording_id)
    labels = sorted(
        {
            str(segment["speaker_session_id"])
            for segment in segments
            if segment["speaker_session_id"] is not None
        }
    )
    kept = sum(segment["speaker_session_id"] is not None for segment in segments)
    previous_stage = database.get_stage(recording_id, "diarization")
    details: dict = {}
    if previous_stage is not None and previous_stage["details_json"]:
        try:
            details = json.loads(previous_stage["details_json"])
        except (TypeError, json.JSONDecodeError):
            details = {}
    details.update({
        "assigned_segments": kept,
        "unassigned_segments": len(segments) - kept,
        "speakers": labels,
    })
    if details_update:
        details.update(details_update)
    database.set_stage(
        recording_id,
        "diarization",
        "completed",
        details=details,
    )
    return details


def _speaker_label(value) -> str:
    if isinstance(value, (int, np.integer)):
        return f"speaker_{int(value):02d}"
    text = str(value).strip()
    if text.isdigit():
        return f"speaker_{int(text):02d}"
    if text.lower().startswith("speaker_"):
        return text.lower()
    return f"speaker_{text}"
