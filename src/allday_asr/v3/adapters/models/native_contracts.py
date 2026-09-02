from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class AsrBackend(Protocol):
    role: str
    model_id: str
    backend_name: str
    model_revision: str | None

    def transcribe(self, audio_path: Path, *, language: str | None) -> Any: ...

    def parameters(self) -> dict[str, Any]: ...

    def close(self) -> None: ...


class DiarizationBackend(Protocol):
    model_id: str
    backend_name: str
    model_revision: str | None

    def ensure_loaded(self) -> None: ...

    def diarize(self, audio_path: Path, **kwargs: Any) -> Any: ...

    def parameters(self) -> dict[str, Any]: ...

    def close(self) -> None: ...


BackendFactory = Callable[[], Any]


@dataclass(frozen=True)
class NativeAsrSettings:
    language: str = "zh"
    window_ms: int = 300_000
    context_ms: int = 5_000


@dataclass(frozen=True)
class NativeDiarizationSettings:
    num_speakers: int | None = None
    min_speakers: int | None = None
    max_speakers: int | None = None
    min_primary_overlap_ratio: float = 0.50


@dataclass(frozen=True)
class _Source:
    asset_id: str
    path: Path
    sha256: str
    session_start_ms: int
    session_end_ms: int
    source_start_ms: int
    source_end_ms: int


@dataclass(frozen=True)
class _Window:
    index: int
    core_start_ms: int
    core_end_ms: int
    analysis_start_ms: int
    analysis_end_ms: int
    sources: tuple[_Source, ...]
