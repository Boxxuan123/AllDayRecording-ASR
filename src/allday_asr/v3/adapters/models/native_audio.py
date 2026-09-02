from __future__ import annotations
import os
import tempfile
import uuid
import wave
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from allday_asr.v3.adapters.audio.tools import extract_clip

from .native_contracts import (
    _Source,
)


@contextmanager
def _temporary_audio(
    sources: Sequence[_Source], start_ms: int, end_ms: int
) -> Iterator[Path]:
    root = Path(tempfile.gettempdir())
    token = uuid.uuid4().hex
    output = root / f"allday-v3-{token}.wav"
    parts: list[Path] = []
    combined = root / f"allday-v3-{token}.combined.wav"
    try:
        cursor = start_ms
        for index, source in enumerate(sources):
            overlap_start = max(start_ms, source.session_start_ms)
            overlap_end = min(end_ms, source.session_end_ms)
            if overlap_end <= overlap_start:
                continue
            if overlap_start != cursor:
                raise ValueError("V3 model window has an uncovered audio range")
            part = root / f"allday-v3-{token}-{index:04d}.wav"
            source_start = (
                source.source_start_ms + overlap_start - source.session_start_ms
            )
            extract_clip(
                source.path,
                part,
                source_start,
                source_start + overlap_end - overlap_start,
            )
            parts.append(part)
            cursor = overlap_end
        if cursor != end_ms or not parts:
            raise ValueError("V3 model window is incomplete")
        if len(parts) == 1:
            os.replace(parts[0], output)
        else:
            _concatenate(parts, combined)
            os.replace(combined, output)
        yield output
    finally:
        output.unlink(missing_ok=True)
        combined.unlink(missing_ok=True)
        for part in parts:
            part.unlink(missing_ok=True)


def _concatenate(parts: Sequence[Path], destination: Path) -> None:
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
                    raise RuntimeError("V3 audio chunks do not share one WAV format")
                writer.writeframes(reader.readframes(reader.getnframes()))
