from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from allday_asr.v3.domain.people import SpeakerEmbedding


@dataclass(frozen=True)
class SpeakerClipInput:
    media_id: str
    storage_key: str
    source_start_ms: int
    source_end_ms: int
    utterance_id: str | None


@dataclass(frozen=True)
class SpeakerTrackInput:
    speaker_track_id: str
    session_id: str
    clips: tuple[SpeakerClipInput, ...]


class SpeakerEmbeddingProvider(Protocol):
    @property
    def model(self) -> str: ...

    @property
    def model_version(self) -> str: ...

    def embed(self, tracks: tuple[SpeakerTrackInput, ...]) -> tuple[SpeakerEmbedding, ...]: ...


__all__ = [
    "SpeakerClipInput",
    "SpeakerEmbeddingProvider",
    "SpeakerTrackInput",
]
