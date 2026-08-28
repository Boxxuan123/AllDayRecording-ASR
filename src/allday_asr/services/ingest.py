from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from allday_asr.audio.tools import probe_audio, sha256_file
from allday_asr.storage.database import Database


@dataclass(frozen=True)
class IngestResult:
    recording: sqlite3.Row
    created: bool


def ingest_recording(
    database: Database,
    source: Path,
    *,
    device: str = "Huawei Watch",
    timezone_name: str = "Asia/Singapore",
) -> IngestResult:
    source = source.resolve(strict=True)
    if not source.is_file():
        raise ValueError(f"不是文件：{source}")

    digest = sha256_file(source)
    existing = database.find_recording_by_hash(digest)
    if existing is not None:
        return IngestResult(existing, created=False)

    metadata = probe_audio(source)
    recording = database.create_recording(
        {
            "source_path": str(source),
            "sha256": digest,
            "device": device,
            "recorded_at": metadata.recorded_at,
            "timezone": timezone_name,
            "duration_ms": metadata.duration_ms,
            "codec": metadata.codec,
            "sample_rate": metadata.sample_rate,
            "channels": metadata.channels,
            "bit_rate": metadata.bit_rate,
            "encoder": metadata.encoder,
        }
    )
    return IngestResult(recording, created=True)

