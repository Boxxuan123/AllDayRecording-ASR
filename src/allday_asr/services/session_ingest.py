from __future__ import annotations

import json
import wave
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from allday_asr.audio.tools import AudioMetadata, probe_audio, sha256_file
from allday_asr.storage.database import Database


CANONICAL_MANIFEST_FORMAT = "AllDayRecording session manifest v1"
PCM_CAPTURE_MANIFEST_FORMAT = "AllDayRecording PCM gap test v1"
PARSER_VERSION = "session-manifest-adapter-v1"
SUPPORTED_MANIFEST_FORMATS = frozenset(
    {CANONICAL_MANIFEST_FORMAT, PCM_CAPTURE_MANIFEST_FORMAT}
)


@dataclass(frozen=True)
class ManifestChunk:
    declared_index: int
    file_name: str
    first_sample: int
    sample_count: int

    @property
    def end_sample(self) -> int:
        return self.first_sample + self.sample_count


@dataclass(frozen=True)
class ParsedSessionManifest:
    manifest_format: str
    session_key: str
    session_started_at: datetime
    timezone_name: str
    device: str
    sample_rate: int
    channels: int
    bits_per_sample: int
    chunks: tuple[ManifestChunk, ...]
    declared_continuity_valid: bool | None
    manifest_path: Path
    manifest_sha256: str
    manifest_byte_size: int


@dataclass(frozen=True)
class SessionIngestSummary:
    session_id: int
    session_key: str
    created: bool
    chunk_count: int
    duration_ms: int
    gap_count: int
    overlap_count: int
    input_fingerprint: str
    manifest_sha256: str


def ingest_session_manifest(
    database: Database,
    manifest_path: Path,
    *,
    device: str = "Huawei Watch",
    timezone_name: str = "Asia/Singapore",
    ingest_method: str = "watch_manual_sync",
) -> SessionIngestSummary:
    """Validate every immutable input before atomically registering one session."""
    if ingest_method not in {"watch_auto", "watch_manual_sync"}:
        raise ValueError("分片清单导入方式必须是 watch_auto 或 watch_manual_sync")
    parsed = parse_session_manifest(
        manifest_path,
        default_device=device,
        default_timezone=timezone_name,
    )
    prepared_chunks, gaps, overlaps = _prepare_chunks(
        parsed,
        ingest_method=ingest_method,
    )
    if parsed.declared_continuity_valid is True and (gaps or overlaps):
        raise ValueError("清单声明连续，但独立检查发现 gap 或 overlap")

    duration_samples = max(value.end_sample for value in parsed.chunks)
    duration_ms = _sample_to_ms(duration_samples, parsed.sample_rate)
    manifest_summary = {
        "chunk_count": len(prepared_chunks),
        "sample_rate": parsed.sample_rate,
        "channels": parsed.channels,
        "bits_per_sample": parsed.bits_per_sample,
        "duration_samples": duration_samples,
        "duration_ms": duration_ms,
        "gaps": [list(value) for value in gaps],
        "overlaps": [list(value) for value in overlaps],
        "declared_continuity_valid": parsed.declared_continuity_valid,
        "validation": "all-files-hashed-and-probed-before-transaction",
    }
    session, created = database.import_closed_session(
        {
            "session_key": parsed.session_key,
            "device": parsed.device,
            "recorded_at": parsed.session_started_at.isoformat(),
            "timezone": parsed.timezone_name,
            "duration_ms": duration_ms,
        },
        {
            "manifest_format": parsed.manifest_format,
            "manifest_path": str(parsed.manifest_path),
            "manifest_sha256": parsed.manifest_sha256,
            "byte_size": parsed.manifest_byte_size,
            "parser_version": PARSER_VERSION,
            "summary": manifest_summary,
        },
        prepared_chunks,
    )
    session_id = int(session["id"])
    return SessionIngestSummary(
        session_id=session_id,
        session_key=str(session["session_key"]),
        created=created,
        chunk_count=len(prepared_chunks),
        duration_ms=int(session["duration_ms"]),
        gap_count=len(gaps),
        overlap_count=len(overlaps),
        input_fingerprint=database.session_input_fingerprint(session_id),
        manifest_sha256=parsed.manifest_sha256,
    )


def parse_session_manifest(
    manifest_path: Path,
    *,
    default_device: str,
    default_timezone: str,
) -> ParsedSessionManifest:
    path = manifest_path.resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"会话清单不是文件：{path}")
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("会话清单不是有效的 UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("会话清单根节点必须是 JSON object")
    manifest_format = str(payload.get("format") or "")
    if manifest_format not in SUPPORTED_MANIFEST_FORMATS:
        raise ValueError(f"不支持的会话清单格式：{manifest_format or '<missing>'}")

    if manifest_format == CANONICAL_MANIFEST_FORMAT:
        audio = _mapping(payload.get("audio"), "audio")
        chunk_values = payload.get("chunks")
        sample_rate = _positive_int(audio.get("sampleRate"), "audio.sampleRate")
        channels = _positive_int(audio.get("channels"), "audio.channels")
        bits_per_sample = _positive_int(
            audio.get("bitsPerSample"), "audio.bitsPerSample"
        )
    else:
        chunk_values = payload.get("segments")
        sample_rate = _positive_int(payload.get("sampleRate"), "sampleRate")
        channels = _positive_int(payload.get("channels"), "channels")
        bits_per_sample = _positive_int(
            payload.get("bitsPerSample"), "bitsPerSample"
        )

    session_started_at, started_key = _parse_started_at(
        payload.get("sessionStartedAt")
    )
    session_key = str(payload.get("sessionKey") or f"watch-session:{started_key}")
    if not session_key.strip() or len(session_key) > 240:
        raise ValueError("sessionKey 为空或过长")
    device = str(payload.get("device") or default_device).strip()
    timezone_name = str(payload.get("timezone") or default_timezone).strip()
    if not device or not timezone_name:
        raise ValueError("device 和 timezone 不能为空")

    if not isinstance(chunk_values, list) or not chunk_values:
        raise ValueError("会话清单没有音频分片")
    chunks: list[ManifestChunk] = []
    indexes: set[int] = set()
    file_names: set[str] = set()
    for position, raw_chunk in enumerate(chunk_values):
        chunk = _mapping(raw_chunk, f"chunk[{position}]")
        index = _nonnegative_int(chunk.get("index"), f"chunk[{position}].index")
        file_name = str(chunk.get("fileName") or "")
        if not file_name:
            raise ValueError(f"chunk[{position}].fileName 不能为空")
        if index in indexes:
            raise ValueError(f"分片 index 重复：{index}")
        if file_name in file_names:
            raise ValueError(f"分片文件名重复：{file_name}")
        indexes.add(index)
        file_names.add(file_name)
        chunks.append(
            ManifestChunk(
                declared_index=index,
                file_name=file_name,
                first_sample=_nonnegative_int(
                    chunk.get("firstSample"), f"chunk[{position}].firstSample"
                ),
                sample_count=_positive_int(
                    chunk.get("sampleCount"), f"chunk[{position}].sampleCount"
                ),
            )
        )
    chunks.sort(key=lambda value: (value.first_sample, value.declared_index))

    declared_count = payload.get("completedSegments")
    if declared_count is not None and _nonnegative_int(
        declared_count, "completedSegments"
    ) != len(chunks):
        raise ValueError("清单 completedSegments 与分片数量不一致")
    declared_total = payload.get("totalSamples")
    computed_total = max(value.end_sample for value in chunks)
    if declared_total is not None and _nonnegative_int(
        declared_total, "totalSamples"
    ) != computed_total:
        raise ValueError("清单 totalSamples 与分片终点不一致")
    declared_continuity = payload.get("continuityValid")
    if declared_continuity is not None and not isinstance(declared_continuity, bool):
        raise ValueError("continuityValid 必须是 boolean")

    return ParsedSessionManifest(
        manifest_format=manifest_format,
        session_key=session_key,
        session_started_at=session_started_at,
        timezone_name=timezone_name,
        device=device,
        sample_rate=sample_rate,
        channels=channels,
        bits_per_sample=bits_per_sample,
        chunks=tuple(chunks),
        declared_continuity_valid=declared_continuity,
        manifest_path=path,
        manifest_sha256=sha256_file(path),
        manifest_byte_size=len(raw),
    )


def _prepare_chunks(
    parsed: ParsedSessionManifest,
    *,
    ingest_method: str,
) -> tuple[list[dict[str, Any]], list[tuple[int, int]], list[tuple[int, int]]]:
    root = parsed.manifest_path.parent
    gaps, overlaps = _continuity_ranges(parsed.chunks)
    prepared: list[dict[str, Any]] = []
    for position, chunk in enumerate(parsed.chunks):
        source_path = _resolve_manifest_file(root, chunk.file_name)
        metadata = probe_audio(source_path)
        _validate_audio_metadata(parsed, chunk, source_path, metadata)
        start_ms = _sample_to_ms(chunk.first_sample, parsed.sample_rate)
        end_ms = _sample_to_ms(chunk.end_sample, parsed.sample_rate)
        duration_ms = _sample_to_ms(chunk.sample_count, parsed.sample_rate)
        if end_ms <= start_ms or duration_ms <= 0:
            raise ValueError(f"分片时间精度低于数据库毫秒坐标：{chunk.file_name}")
        previous_end = parsed.chunks[position - 1].end_sample if position else 0
        if chunk.first_sample > previous_end:
            continuity = "gap"
        elif chunk.first_sample < previous_end:
            continuity = "overlap"
        else:
            continuity = "continuous"
        recorded_at = parsed.session_started_at + timedelta(
            seconds=chunk.first_sample / parsed.sample_rate
        )
        digest = sha256_file(source_path)
        common = {
            "source_path": str(source_path),
            "original_filename": source_path.name,
            "byte_size": source_path.stat().st_size,
            "recorded_at": recorded_at.isoformat(),
            "timezone": parsed.timezone_name,
            "device": parsed.device,
            "ingest_method": ingest_method,
            "sample_rate": parsed.sample_rate,
            "sample_count": chunk.sample_count,
        }
        prepared.append(
            {
                "source": {
                    **common,
                    "sha256": digest,
                    "container": source_path.suffix.lower().lstrip(".") or None,
                    "codec": metadata.codec,
                    "channels": metadata.channels,
                    "bit_rate": metadata.bit_rate,
                    "encoder": metadata.encoder,
                    "duration_ms": duration_ms,
                },
                "instance": {
                    **common,
                    "instance_key": (
                        f"{parsed.session_key}:chunk:{chunk.declared_index}"
                    ),
                },
                "mapping": {
                    "chunk_index": position,
                    "session_start_ms": start_ms,
                    "session_end_ms": end_ms,
                    "source_start_ms": 0,
                    "source_end_ms": duration_ms,
                    "session_start_sample": chunk.first_sample,
                    "session_end_sample": chunk.end_sample,
                    "source_start_sample": 0,
                    "source_end_sample": chunk.sample_count,
                    "timeline_sample_rate": parsed.sample_rate,
                    "continuity_status": continuity,
                },
            }
        )
    return prepared, gaps, overlaps


def _validate_audio_metadata(
    manifest: ParsedSessionManifest,
    chunk: ManifestChunk,
    source_path: Path,
    metadata: AudioMetadata,
) -> None:
    if metadata.sample_rate != manifest.sample_rate:
        raise ValueError(f"分片采样率与清单不一致：{chunk.file_name}")
    if metadata.channels != manifest.channels:
        raise ValueError(f"分片声道数与清单不一致：{chunk.file_name}")
    expected_ms = _sample_to_ms(chunk.sample_count, manifest.sample_rate)
    if abs(int(metadata.duration_ms) - expected_ms) > 2:
        raise ValueError(f"分片时长与清单 sampleCount 不一致：{chunk.file_name}")
    if source_path.suffix.lower() == ".wav":
        try:
            with wave.open(str(source_path), "rb") as reader:
                if reader.getframerate() != manifest.sample_rate:
                    raise ValueError(f"WAV 采样率不一致：{chunk.file_name}")
                if reader.getnchannels() != manifest.channels:
                    raise ValueError(f"WAV 声道数不一致：{chunk.file_name}")
                if reader.getsampwidth() * 8 != manifest.bits_per_sample:
                    raise ValueError(f"WAV 位深不一致：{chunk.file_name}")
                if reader.getnframes() != chunk.sample_count:
                    raise ValueError(f"WAV frame 数与清单不一致：{chunk.file_name}")
        except wave.Error as exc:
            raise ValueError(f"WAV 文件损坏：{chunk.file_name}") from exc


def _continuity_ranges(
    chunks: Sequence[ManifestChunk],
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    gaps: list[tuple[int, int]] = []
    overlaps: list[tuple[int, int]] = []
    cursor = 0
    for chunk in chunks:
        if chunk.first_sample > cursor:
            gaps.append((cursor, chunk.first_sample))
        elif chunk.first_sample < cursor:
            overlaps.append((chunk.first_sample, min(cursor, chunk.end_sample)))
        cursor = max(cursor, chunk.end_sample)
    return gaps, overlaps


def _resolve_manifest_file(root: Path, file_name: str) -> Path:
    relative = Path(file_name)
    if relative.is_absolute():
        raise ValueError(f"分片路径不能是绝对路径：{file_name}")
    candidate = (root / relative).resolve(strict=True)
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"分片路径越出清单目录：{file_name}") from exc
    if not candidate.is_file():
        raise ValueError(f"分片不是文件：{file_name}")
    return candidate


def _parse_started_at(value: Any) -> tuple[datetime, str]:
    if isinstance(value, bool) or value is None:
        raise ValueError("sessionStartedAt 缺失或无效")
    if isinstance(value, (int, float)):
        milliseconds = int(value)
        if milliseconds < 0:
            raise ValueError("sessionStartedAt 不能为负数")
        return (
            datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc),
            str(milliseconds),
        )
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("sessionStartedAt 必须是 epoch milliseconds 或 ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("ISO sessionStartedAt 必须包含时区")
    normalized = parsed.astimezone(timezone.utc)
    return normalized, normalized.isoformat()


def _sample_to_ms(value: int, sample_rate: int) -> int:
    return round(value * 1000 / sample_rate)


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} 必须是 JSON object")
    return value


def _positive_int(value: Any, name: str) -> int:
    result = _nonnegative_int(value, name)
    if result < 1:
        raise ValueError(f"{name} 必须大于 0")
    return result


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数")
    if value < 0:
        raise ValueError(f"{name} 不能小于 0")
    return value
