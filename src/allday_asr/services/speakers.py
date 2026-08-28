from __future__ import annotations

import wave
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from allday_asr.audio.tools import extract_clip
from allday_asr.paths import STATE_DIR, recording_output_dir
from allday_asr.storage.database import Database


@dataclass(frozen=True)
class SpeakerSamplesSummary:
    directory: Path
    manifest: Path
    speakers: dict[str, int]
    compilations: dict[str, Path]


@dataclass(frozen=True)
class MarkSelfSummary:
    profile_id: int
    speaker: str
    assigned_segments: int
    voiceprint_path: Path


@dataclass(frozen=True)
class UnmarkSelfSummary:
    profile_id: int
    cleared_segments: int
    profile_deleted: bool
    voiceprint_path: Path | None
    voiceprint_deleted: bool


def export_speaker_samples(
    database: Database,
    recording_id: int,
    *,
    per_speaker: int = 3,
    speaker: str | None = None,
) -> SpeakerSamplesSummary:
    recording = database.get_recording(recording_id)
    source = Path(recording["source_path"])
    groups: dict[str, list] = defaultdict(list)
    for segment in database.all_segments(recording_id, completed_only=True):
        label = segment["speaker_session_id"]
        if label and (speaker is None or label.lower() == speaker.lower()):
            groups[label].append(segment)
    if not groups:
        if speaker:
            raise RuntimeError(f"当前录音中没有 {speaker} 的已完成片段")
        raise RuntimeError("尚无说话人标签，请先执行 diarize")

    root = recording_output_dir(recording_id) / "speaker-samples"
    lines = [f"# Recording {recording_id} 说话人试听样本", ""]
    counts: dict[str, int] = {}
    compilations: dict[str, Path] = {}
    for label in sorted(groups):
        selected = sorted(
            groups[label],
            key=lambda item: int(item["end_ms"]) - int(item["start_ms"]),
            reverse=True,
        )[:per_speaker]
        counts[label] = len(selected)
        lines.extend([f"## {label}", ""])
        sample_paths: list[Path] = []
        for segment in selected:
            destination = root / label / f"segment-{segment['id']}.wav"
            extract_clip(
                source,
                destination,
                int(segment["start_ms"]),
                int(segment["end_ms"]),
            )
            sample_paths.append(destination)
            text = (segment["text_display"] or "").strip()
            relative = destination.relative_to(root).as_posix()
            lines.append(f"- [{destination.name}]({relative})：{text}")
            lines.append(
                f"  - offset {segment['start_ms'] / 1000:.3f}s–{segment['end_ms'] / 1000:.3f}s"
            )
        compilation = root / label / f"{label}-compilation.wav"
        _combine_wav_samples(sample_paths, compilation)
        compilations[label] = compilation
        compilation_relative = compilation.relative_to(root).as_posix()
        lines.extend(
            [
                "",
                f"连续试听（仅拼接上述 {len(sample_paths)} 段，中间留 0.4 秒静音）：",
                f"[{compilation.name}]({compilation_relative})",
            ]
        )
        lines.append("")
    manifest = root / "README.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("\n".join(lines), encoding="utf-8")
    return SpeakerSamplesSummary(root, manifest, counts, compilations)


def _combine_wav_samples(
    sources: list[Path], destination: Path, *, silence_seconds: float = 0.4
) -> Path:
    if not sources:
        raise ValueError("至少需要一个试听片段")
    destination.parent.mkdir(parents=True, exist_ok=True)
    parameters: tuple[int, int, int] | None = None
    chunks: list[bytes] = []
    for source in sources:
        with wave.open(str(source), "rb") as reader:
            current = (reader.getnchannels(), reader.getsampwidth(), reader.getframerate())
            if parameters is None:
                parameters = current
            elif current != parameters:
                raise RuntimeError("试听片段的 WAV 参数不一致，无法拼接")
            chunks.append(reader.readframes(reader.getnframes()))
    assert parameters is not None
    channels, sample_width, frame_rate = parameters
    silence = b"\x00" * round(silence_seconds * frame_rate) * channels * sample_width
    temporary = destination.with_suffix(".tmp.wav")
    with wave.open(str(temporary), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(sample_width)
        writer.setframerate(frame_rate)
        for index, chunk in enumerate(chunks):
            if index:
                writer.writeframes(silence)
            writer.writeframes(chunk)
    temporary.replace(destination)
    return destination


def mark_speaker_as_self(
    database: Database,
    recording_id: int,
    speaker: str,
    *,
    display_name: str = "我",
    confirmed_pure: bool = False,
) -> MarkSelfSummary:
    normalized_speaker = speaker.lower()
    if not normalized_speaker.startswith("speaker_"):
        raise ValueError("speaker 必须形如 speaker_03")
    try:
        int(normalized_speaker.split("_", 1)[1])
    except ValueError as exc:
        raise ValueError("speaker 必须形如 speaker_03") from exc
    if not confirmed_pure:
        raise RuntimeError("必须先逐段试听确认聚类纯净，并显式传入 confirmed_pure=True")

    candidate_segments = [
        segment
        for segment in database.all_segments(recording_id, completed_only=True)
        if segment["speaker_session_id"] == normalized_speaker
    ]
    candidate_speech_ms = sum(
        int(segment["end_ms"]) - int(segment["start_ms"])
        for segment in candidate_segments
    )
    if len(candidate_segments) < 3 or candidate_speech_ms < 5_000:
        raise RuntimeError("该聚类的可靠样本不足，不能登记为本人")

    profile = database.get_self_profile()
    if profile is None:
        raise RuntimeError("尚未登记独立本人声纹，请先执行 enroll-self")
    final_path = Path(profile["embedding_path"])
    if not final_path.is_file():
        raise RuntimeError(f"本人声纹文件不存在：{final_path}")
    assigned = database.assign_person_to_speaker(
        recording_id,
        normalized_speaker,
        int(profile["id"]),
        score=1.0,
    )
    if assigned == 0:
        raise RuntimeError(f"当前录音中没有 {normalized_speaker}")
    return MarkSelfSummary(
        profile_id=int(profile["id"]),
        speaker=normalized_speaker,
        assigned_segments=assigned,
        voiceprint_path=final_path,
    )


def unmark_self(database: Database, recording_id: int) -> UnmarkSelfSummary:
    """Undo a self identity assignment without touching ASR or diarization results."""
    database.get_recording(recording_id)
    profile = database.get_self_profile()
    if profile is None:
        raise RuntimeError("当前没有已登记的本人档案")

    profile_id = int(profile["id"])
    raw_voiceprint = profile["embedding_path"]
    voiceprint_path = Path(raw_voiceprint).resolve() if raw_voiceprint else None
    if voiceprint_path is not None:
        allowed_root = (STATE_DIR / "voiceprints").resolve()
        if not voiceprint_path.is_relative_to(allowed_root):
            raise RuntimeError(f"拒绝删除 voiceprints 目录之外的声纹文件：{voiceprint_path}")

    cleared = database.clear_person_assignments(recording_id, profile_id)
    return UnmarkSelfSummary(
        profile_id=profile_id,
        cleared_segments=cleared,
        profile_deleted=False,
        voiceprint_path=voiceprint_path,
        voiceprint_deleted=False,
    )
