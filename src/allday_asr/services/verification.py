from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from allday_asr.asr.funasr_backend import FunASRBackend
from allday_asr.audio.embeddings import l2_normalize, normalize_speech_level
from allday_asr.audio.tools import extract_clip
from allday_asr.paths import recording_output_dir
from allday_asr.storage.database import Database


@dataclass(frozen=True)
class SelfCandidateSummary:
    recording_id: int
    scored_segments: int
    strict_candidates: int
    threshold: float
    directory: Path
    manifest_path: Path
    json_path: Path


def export_self_candidates(
    database: Database,
    recording_id: int,
    *,
    device: str = "auto",
    threshold: float = 0.70,
    min_segment_ms: int = 2_000,
    top: int = 20,
    window_seconds: float = 4.0,
) -> SelfCandidateSummary:
    if not 0 < threshold <= 1:
        raise ValueError("threshold 必须在 0 到 1 之间")
    profile = database.get_self_profile()
    if profile is None or not profile["embedding_path"]:
        raise RuntimeError("尚未登记本人声纹，请先执行 enroll-self")
    voiceprint_path = Path(profile["embedding_path"])
    if not voiceprint_path.is_file():
        raise RuntimeError(f"本人声纹文件不存在：{voiceprint_path}")
    voiceprint = np.load(voiceprint_path)
    profile_embeddings = l2_normalize(voiceprint["embeddings"])
    centroid = np.asarray(voiceprint["centroid"], dtype=np.float32)

    recording = database.get_recording(recording_id)
    normalized_path = Path(recording["normalized_path"] or "")
    if not normalized_path.is_file():
        raise RuntimeError("尚无标准化音频，请先执行 process")
    source_path = Path(recording["source_path"])
    segments = [
        segment
        for segment in database.all_segments(recording_id, completed_only=True)
        if int(segment["end_ms"]) - int(segment["start_ms"]) >= min_segment_ms
    ]
    if not segments:
        raise RuntimeError("没有达到最小时长的已转写片段")

    windows: list[np.ndarray] = []
    window_segment_ids: list[int] = []
    with sf.SoundFile(normalized_path) as audio:
        if audio.samplerate != 16_000 or audio.channels != 1:
            raise RuntimeError("标准化录音必须是 16 kHz 单声道")
        for segment in segments:
            start_frame = round(int(segment["start_ms"]) * audio.samplerate / 1000)
            frame_count = round(
                (int(segment["end_ms"]) - int(segment["start_ms"]))
                * audio.samplerate
                / 1000
            )
            audio.seek(start_frame)
            samples = audio.read(frame_count, dtype="float32", always_2d=False)
            for window in split_verification_windows(
                samples,
                audio.samplerate,
                target_seconds=window_seconds,
                min_seconds=min(2.0, min_segment_ms / 1000),
            ):
                windows.append(normalize_speech_level(window))
                window_segment_ids.append(int(segment["id"]))

    backend = FunASRBackend(device=device)
    embeddings = l2_normalize(
        backend.extract_speaker_embeddings(windows, batch_size=16)
    )
    centroid_scores = embeddings @ centroid
    reference_scores = embeddings @ profile_embeddings.T
    top_k = min(3, profile_embeddings.shape[0])
    top_reference_scores = np.partition(reference_scores, -top_k, axis=1)[:, -top_k:].mean(axis=1)
    combined_scores = 0.7 * centroid_scores + 0.3 * top_reference_scores

    scores_by_segment: dict[int, list[float]] = {}
    for segment_id, score in zip(window_segment_ids, combined_scores, strict=True):
        scores_by_segment.setdefault(segment_id, []).append(float(score))
    rows: list[dict] = []
    for segment in segments:
        values = scores_by_segment[int(segment["id"])]
        minimum = min(values)
        median = float(np.median(values))
        maximum = max(values)
        rows.append(
            {
                "segment_id": int(segment["id"]),
                "start_ms": int(segment["start_ms"]),
                "end_ms": int(segment["end_ms"]),
                "duration_ms": int(segment["end_ms"]) - int(segment["start_ms"]),
                "speaker_session_id": segment["speaker_session_id"],
                "text": segment["text_display"] or "",
                "window_count": len(values),
                "score_min": minimum,
                "score_median": median,
                "score_max": maximum,
                "strict_candidate": minimum >= threshold,
            }
        )
    rows.sort(key=lambda item: (item["score_min"], item["score_median"]), reverse=True)

    root = recording_output_dir(recording_id) / "self-candidates"
    root.mkdir(parents=True, exist_ok=True)
    for stale_audio in root.glob("rank-*-segment-*.wav"):
        stale_audio.unlink()
    saved_annotations: dict[int, dict] = {}
    annotations_path = root / "annotations.json"
    if annotations_path.is_file():
        payload = json.loads(annotations_path.read_text(encoding="utf-8"))
        saved_annotations = {
            int(item["segment_id"]): item for item in payload.get("annotations", [])
        }
    lines = [
        f"# Recording {recording_id} 本人候选片段",
        "",
        "> 这些结果只用于人工试听校准，尚未写入本人身份。",
        "",
        f"严格阈值：`{threshold:.3f}`；要求片段内每个窗口均达到阈值。",
        "",
    ]
    for rank, row in enumerate(rows[:top], start=1):
        destination = root / f"rank-{rank:02d}-segment-{row['segment_id']}.wav"
        extract_clip(
            source_path,
            destination,
            row["start_ms"],
            row["end_ms"],
        )
        row["review_audio"] = destination.name
        status = "通过严格阈值" if row["strict_candidate"] else "仅供比较"
        annotation = saved_annotations.get(row["segment_id"])
        annotation_suffix = f"（{annotation['raw_label']}）" if annotation else ""
        lines.extend(
            [
                f"## {rank}. segment {row['segment_id']} · {status}{annotation_suffix}",
                "",
                f"[{destination.name}]({destination.name})",
                "",
                f"- score(min/median/max): {row['score_min']:.3f} / "
                f"{row['score_median']:.3f} / {row['score_max']:.3f}",
                f"- 原标签：{row['speaker_session_id'] or 'unknown'}",
                f"- 文字：{row['text']}",
            ]
        )
        if annotation and annotation.get("note"):
            lines.append(f"- 修订：{annotation['note']}")
        lines.append("")
    manifest_path = root / "README.md"
    json_path = root / "scores.json"
    manifest_path.write_text("\n".join(lines), encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {
                "recording_id": recording_id,
                "threshold": threshold,
                "min_segment_ms": min_segment_ms,
                "scored_segments": len(rows),
                "strict_candidates": sum(item["strict_candidate"] for item in rows),
                "segments": rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return SelfCandidateSummary(
        recording_id=recording_id,
        scored_segments=len(rows),
        strict_candidates=sum(item["strict_candidate"] for item in rows),
        threshold=threshold,
        directory=root,
        manifest_path=manifest_path,
        json_path=json_path,
    )


def split_verification_windows(
    samples: np.ndarray,
    sample_rate: int,
    *,
    target_seconds: float = 4.0,
    min_seconds: float = 2.0,
) -> list[np.ndarray]:
    value = np.asarray(samples, dtype=np.float32)
    target = round(target_seconds * sample_rate)
    minimum = round(min_seconds * sample_rate)
    if len(value) < minimum:
        return []
    chunks = [value[index : index + target] for index in range(0, len(value), target)]
    if len(chunks) > 1 and len(chunks[-1]) < minimum:
        remainder = chunks.pop()
        chunks[-1] = np.concatenate([chunks[-1], remainder])
    return chunks
