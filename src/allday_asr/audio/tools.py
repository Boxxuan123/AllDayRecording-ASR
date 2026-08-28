from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AudioToolError(RuntimeError):
    pass


@dataclass(frozen=True)
class AudioMetadata:
    duration_ms: int
    codec: str | None
    sample_rate: int | None
    channels: int | None
    bit_rate: int | None
    recorded_at: str
    encoder: str | None


def executable_version(name: str) -> str:
    executable = shutil.which(name)
    if not executable:
        raise AudioToolError(f"找不到 {name}，请确认它已加入 PATH")
    completed = subprocess.run(
        [executable, "-version"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise AudioToolError(completed.stderr.strip() or f"{name} 执行失败")
    return completed.stdout.splitlines()[0].strip()


def probe_audio(path: Path) -> AudioMetadata:
    source = path.resolve(strict=True)
    executable = shutil.which("ffprobe")
    if not executable:
        raise AudioToolError("找不到 ffprobe，请确认 FFmpeg 已加入 PATH")
    completed = subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-show_entries",
            "format=duration,bit_rate:format_tags=creation_time,encoder:stream=codec_name,codec_type,sample_rate,channels,bit_rate",
            "-of",
            "json",
            str(source),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise AudioToolError(completed.stderr.strip() or "ffprobe 无法读取音频")

    try:
        payload: dict[str, Any] = json.loads(completed.stdout)
        format_info = payload.get("format", {})
        audio_stream = next(
            item for item in payload.get("streams", []) if item.get("codec_type") == "audio"
        )
    except (json.JSONDecodeError, StopIteration, TypeError) as exc:
        raise AudioToolError("ffprobe 返回了无法解析的音频信息") from exc

    tags = format_info.get("tags", {}) or {}
    creation_time = tags.get("creation_time")
    if not creation_time:
        creation_time = datetime.fromtimestamp(source.stat().st_mtime, tz=timezone.utc).isoformat()

    duration_ms = round(float(format_info.get("duration", 0)) * 1000)
    stream_bit_rate = audio_stream.get("bit_rate")
    format_bit_rate = format_info.get("bit_rate")

    return AudioMetadata(
        duration_ms=duration_ms,
        codec=audio_stream.get("codec_name"),
        sample_rate=_optional_int(audio_stream.get("sample_rate")),
        channels=_optional_int(audio_stream.get("channels")),
        bit_rate=_optional_int(stream_bit_rate or format_bit_rate),
        recorded_at=str(creation_time),
        encoder=tags.get("encoder"),
    )


def sha256_file(path: Path, block_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(block_size):
            digest.update(block)
    return digest.hexdigest()


def normalize_to_wav(source: Path, destination: Path) -> Path:
    """Create a 16 kHz mono PCM WAV atomically without modifying the source."""
    source = source.resolve(strict=True)
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_size > 44:
        return destination

    executable = shutil.which("ffmpeg")
    if not executable:
        raise AudioToolError("找不到 ffmpeg，请确认 FFmpeg 已加入 PATH")

    temporary = destination.with_suffix(".tmp.wav")
    if temporary.exists():
        temporary.unlink()
    completed = subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(temporary),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        if temporary.exists():
            temporary.unlink()
        raise AudioToolError(completed.stderr.strip() or "音频标准化失败")
    os.replace(temporary, destination)
    return destination


def extract_clip(source: Path, destination: Path, start_ms: int, end_ms: int) -> Path:
    if end_ms <= start_ms:
        raise ValueError("end_ms 必须大于 start_ms")
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    executable = shutil.which("ffmpeg")
    if not executable:
        raise AudioToolError("找不到 ffmpeg")
    completed = subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-y",
            "-ss",
            f"{start_ms / 1000:.3f}",
            "-to",
            f"{end_ms / 1000:.3f}",
            "-i",
            str(source.resolve(strict=True)),
            "-ac",
            "1",
            "-ar",
            "16000",
            str(destination),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise AudioToolError(completed.stderr.strip() or "截取音频失败")
    return destination


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)

