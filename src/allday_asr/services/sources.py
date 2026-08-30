from __future__ import annotations

import os
import tempfile
import uuid
import wave
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from allday_asr.audio.tools import AudioMetadata, extract_clip, probe_audio, sha256_file
from allday_asr.domain.hashing import canonical_json_sha256
from allday_asr.storage.database import Database


@dataclass(frozen=True)
class SourceIntegrityResult:
    source_object_id: int
    path: Path
    status: str
    expected_sha256: str
    actual_sha256: str | None
    expected_byte_size: int | None
    actual_byte_size: int | None
    mismatches: dict[str, dict[str, object]]
    error: str | None = None


@dataclass(frozen=True)
class SourceSlice:
    source_object_id: int
    source_instance_id: int
    source_path: Path
    source_sha256: str
    session_start_ms: int
    session_end_ms: int
    source_start_ms: int
    source_end_ms: int

    @property
    def duration_ms(self) -> int:
        return self.session_end_ms - self.session_start_ms


@dataclass(frozen=True)
class LogicalWindow:
    session_id: int
    index: int
    core_start_ms: int
    core_end_ms: int
    analysis_start_ms: int
    analysis_end_ms: int
    slices: tuple[SourceSlice, ...]
    uncovered_ranges: tuple[tuple[int, int], ...]

    @property
    def duration_ms(self) -> int:
        return self.analysis_end_ms - self.analysis_start_ms

    @property
    def coverage_complete(self) -> bool:
        return not self.uncovered_ranges


def audit_source_object(database: Database, source_object_id: int) -> SourceIntegrityResult:
    source = database.get_source_object(source_object_id)
    path = Path(str(source["source_path"]))
    expected_sha256 = str(source["sha256"])
    expected_size = (
        int(source["byte_size"]) if source["byte_size"] is not None else None
    )
    actual_sha256: str | None = None
    actual_size: int | None = None
    mismatches: dict[str, dict[str, object]] = {}
    error: str | None = None

    if not path.is_file():
        status = "missing"
        error = f"原始音频不存在：{path}"
        details = {"mismatches": mismatches, "error": error}
    else:
        try:
            actual_size = path.stat().st_size
            actual_sha256 = sha256_file(path)
            metadata = probe_audio(path)
            if actual_sha256 != expected_sha256:
                mismatches["sha256"] = {
                    "expected": expected_sha256,
                    "actual": actual_sha256,
                }
            if expected_size is not None and actual_size != expected_size:
                mismatches["byte_size"] = {
                    "expected": expected_size,
                    "actual": actual_size,
                }
            _compare_metadata(source, metadata, mismatches)
            status = "mismatch" if mismatches else "verified"
            details = {
                "mismatches": mismatches,
                "metadata": {
                    "duration_ms": metadata.duration_ms,
                    "codec": metadata.codec,
                    "sample_rate": metadata.sample_rate,
                    "channels": metadata.channels,
                    "bit_rate": metadata.bit_rate,
                    "recorded_at": metadata.recorded_at,
                    "encoder": metadata.encoder,
                },
            }
        except Exception as exc:
            status = "error"
            error = repr(exc)
            details = {"mismatches": mismatches, "error": error}

    database.record_source_integrity_audit(
        source_object_id,
        status=status,
        actual_sha256=actual_sha256,
        actual_byte_size=actual_size,
        details=details,
    )
    return SourceIntegrityResult(
        source_object_id=source_object_id,
        path=path,
        status=status,
        expected_sha256=expected_sha256,
        actual_sha256=actual_sha256,
        expected_byte_size=expected_size,
        actual_byte_size=actual_size,
        mismatches=mismatches,
        error=error,
    )


def audit_all_sources(database: Database) -> list[SourceIntegrityResult]:
    return [
        audit_source_object(database, int(source["id"]))
        for source in database.list_source_objects()
    ]


def plan_logical_windows(
    database: Database,
    session_id: int,
    *,
    window_ms: int = 300_000,
    context_ms: int = 5_000,
) -> list[LogicalWindow]:
    if window_ms <= 0:
        raise ValueError("window_ms 必须大于 0")
    if context_ms < 0:
        raise ValueError("context_ms 不能小于 0")
    session = database.get_recording_session(session_id)
    duration_ms = int(session["duration_ms"])
    if duration_ms <= 0:
        return []
    sources = database.list_session_sources(session_id)
    windows: list[LogicalWindow] = []
    core_start = 0
    index = 0
    while core_start < duration_ms:
        core_end = min(duration_ms, core_start + window_ms)
        analysis_start = max(0, core_start - context_ms)
        analysis_end = min(duration_ms, core_end + context_ms)
        slices, gaps = _resolve_source_slices(sources, analysis_start, analysis_end)
        windows.append(
            LogicalWindow(
                session_id=session_id,
                index=index,
                core_start_ms=core_start,
                core_end_ms=core_end,
                analysis_start_ms=analysis_start,
                analysis_end_ms=analysis_end,
                slices=tuple(slices),
                uncovered_ranges=tuple(gaps),
            )
        )
        core_start = core_end
        index += 1
    return windows


def resolve_session_slices(
    database: Database, session_id: int, start_ms: int, end_ms: int
) -> tuple[tuple[SourceSlice, ...], tuple[tuple[int, int], ...]]:
    session = database.get_recording_session(session_id)
    if start_ms < 0 or end_ms <= start_ms:
        raise ValueError("会话时间范围无效")
    if end_ms > int(session["duration_ms"]):
        raise ValueError("会话时间范围超过录音持续时间")
    slices, gaps = _resolve_source_slices(
        database.list_session_sources(session_id), start_ms, end_ms
    )
    return tuple(slices), tuple(gaps)


def logical_window_cache_key(
    window: LogicalWindow, *, transform: str = "pcm16-16khz-mono-v1"
) -> str:
    payload = {
        "session_id": window.session_id,
        "analysis_start_ms": window.analysis_start_ms,
        "analysis_end_ms": window.analysis_end_ms,
        "transform": transform,
        "slices": [
            {
                "source_object_id": item.source_object_id,
                "source_instance_id": item.source_instance_id,
                "sha256": item.source_sha256,
                "source_start_ms": item.source_start_ms,
                "source_end_ms": item.source_end_ms,
            }
            for item in window.slices
        ],
    }
    return canonical_json_sha256(payload)


def materialize_logical_window(
    window: LogicalWindow,
    destination: Path,
    *,
    audio_filter: str | None = None,
) -> Path:
    if not window.coverage_complete:
        raise RuntimeError(
            f"逻辑窗口 {window.index} 存在未覆盖时间范围：{window.uncovered_ranges}"
        )
    if not window.slices:
        raise RuntimeError(f"逻辑窗口 {window.index} 没有原始音频来源")
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"不会覆盖已有窗口文件：{destination}")

    token = uuid.uuid4().hex
    parts = [
        destination.parent / f".{destination.name}.{token}.part-{position:04d}.wav"
        for position in range(len(window.slices))
    ]
    combined = destination.parent / f".{destination.name}.{token}.combined.wav"
    temporary_paths = [*parts, combined]
    try:
        for position, source_slice in enumerate(window.slices):
            part = parts[position]
            extract_clip(
                source_slice.source_path,
                part,
                source_slice.source_start_ms,
                source_slice.source_end_ms,
                audio_filter=audio_filter,
            )
        if len(parts) == 1:
            os.replace(parts[0], combined)
        else:
            _concatenate_pcm_wavs(parts, combined)
        os.replace(combined, destination)
    finally:
        for path in temporary_paths:
            path.unlink(missing_ok=True)
    return destination


@contextmanager
def temporary_logical_window(
    window: LogicalWindow, *, audio_filter: str | None = None
) -> Iterator[Path]:
    path = Path(tempfile.gettempdir()) / (
        f"allday-asr-session-{window.session_id}-window-{window.index:06d}-"
        f"{uuid.uuid4().hex}.wav"
    )
    try:
        materialize_logical_window(window, path, audio_filter=audio_filter)
        yield path
    finally:
        path.unlink(missing_ok=True)


def _compare_metadata(
    source, metadata: AudioMetadata, mismatches: dict[str, dict[str, object]]
) -> None:
    expected_duration = int(source["duration_ms"])
    if abs(metadata.duration_ms - expected_duration) > 250:
        mismatches["duration_ms"] = {
            "expected": expected_duration,
            "actual": metadata.duration_ms,
        }
    for field in ("codec", "sample_rate", "channels"):
        expected = source[field]
        actual = getattr(metadata, field)
        if expected is not None and actual != expected:
            mismatches[field] = {"expected": expected, "actual": actual}


def _resolve_source_slices(
    rows, start_ms: int, end_ms: int
) -> tuple[list[SourceSlice], list[tuple[int, int]]]:
    slices: list[SourceSlice] = []
    gaps: list[tuple[int, int]] = []
    cursor = start_ms
    for row in rows:
        row_start = int(row["session_start_ms"])
        row_end = int(row["session_end_ms"])
        if row_end <= cursor or row_start >= end_ms:
            continue
        overlap_start = max(cursor, start_ms, row_start)
        overlap_end = min(end_ms, row_end)
        if overlap_start > cursor:
            gaps.append((cursor, overlap_start))
        if overlap_end <= overlap_start:
            continue
        source_start = int(row["source_start_ms"]) + (overlap_start - row_start)
        source_end = source_start + (overlap_end - overlap_start)
        slices.append(
            SourceSlice(
                source_object_id=int(row["source_object_id"]),
                source_instance_id=int(row["source_instance_id"]),
                source_path=Path(str(row["source_path"])),
                source_sha256=str(row["sha256"]),
                session_start_ms=overlap_start,
                session_end_ms=overlap_end,
                source_start_ms=source_start,
                source_end_ms=source_end,
            )
        )
        cursor = overlap_end
        if cursor >= end_ms:
            break
    if cursor < end_ms:
        gaps.append((cursor, end_ms))
    return slices, gaps


def _concatenate_pcm_wavs(parts: list[Path], destination: Path) -> None:
    parameters: tuple[int, int, int, str, str] | None = None
    with wave.open(str(destination), "wb") as writer:
        for part in parts:
            with wave.open(str(part), "rb") as reader:
                current = (
                    reader.getnchannels(),
                    reader.getsampwidth(),
                    reader.getframerate(),
                    reader.getcomptype(),
                    reader.getcompname(),
                )
                if parameters is None:
                    parameters = current
                    writer.setparams((*current[:3], 0, *current[3:]))
                elif current != parameters:
                    raise RuntimeError(f"逻辑窗口的 WAV 分片格式不一致：{part}")
                writer.writeframes(reader.readframes(reader.getnframes()))
