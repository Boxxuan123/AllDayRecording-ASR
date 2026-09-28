from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import numpy as np
import soundfile as sf

from allday_asr.v3.adapters.audio.tools import extract_clip
from allday_asr.v3.adapters.models.funasr import FunASRBackend
from allday_asr.v3.adapters.files import ContentAddressedStore
from allday_asr.v3.domain.people import RepresentativeClip, SpeakerEmbedding
from allday_asr.v3.ports.speaker_embeddings import SpeakerTrackInput


class _EmbeddingBackend(Protocol):
    def extract_speaker_embeddings(
        self, samples: list[np.ndarray], *, batch_size: int = 8
    ) -> np.ndarray: ...


class FunASRSpeakerEmbeddingProvider:
    """Local CAM++ adapter. Audio never leaves the content-addressed store."""

    def __init__(
        self,
        audio_store: ContentAddressedStore,
        *,
        device: str = "auto",
        backend_factory: Callable[[], _EmbeddingBackend] | None = None,
        temp_root: Path | None = None,
    ) -> None:
        self._audio_store = audio_store
        self._backend_factory = backend_factory or (lambda: FunASRBackend(device=device))
        self._temp_root = temp_root

    @property
    def model(self) -> str:
        return "FunASR/CAM++"

    @property
    def model_version(self) -> str:
        return "v1-local"

    def embed(self, tracks: tuple[SpeakerTrackInput, ...]) -> tuple[SpeakerEmbedding, ...]:
        if not tracks:
            return ()
        samples: list[np.ndarray] = []
        sample_tracks: list[int] = []
        representatives: dict[int, list[RepresentativeClip]] = {}
        durations: dict[int, int] = {}
        with _clip_directory(self._temp_root) as root:
            for track_index, track in enumerate(tracks):
                for clip_index, clip in enumerate(track.clips):
                    destination = root / f"{track_index}-{clip_index}.wav"
                    extract_clip(
                        self._audio_store.path_for(clip.storage_key),
                        destination,
                        clip.source_start_ms,
                        clip.source_end_ms,
                    )
                    waveform, sample_rate = sf.read(
                        destination, dtype="float32", always_2d=False
                    )
                    if sample_rate != 16_000:
                        raise RuntimeError("speaker clip must be normalized to 16 kHz")
                    array = np.asarray(waveform, dtype=np.float32)
                    if array.ndim == 2:
                        array = array.mean(axis=1)
                    if array.size < 8_000:
                        continue
                    samples.append(array)
                    sample_tracks.append(track_index)
                    representatives.setdefault(track_index, []).append(
                        RepresentativeClip(
                            media_id=clip.media_id,
                            start_ms=clip.source_start_ms,
                            end_ms=clip.source_end_ms,
                            utterance_id=clip.utterance_id,
                        )
                    )
                    durations[track_index] = durations.get(track_index, 0) + (
                        clip.source_end_ms - clip.source_start_ms
                    )
        if not samples:
            return ()
        raw = np.asarray(
            self._backend_factory().extract_speaker_embeddings(samples, batch_size=8),
            dtype=np.float32,
        )
        if raw.ndim != 2 or raw.shape[0] != len(samples):
            raise RuntimeError("speaker embedding result shape is invalid")
        grouped: dict[int, list[np.ndarray]] = {}
        for track_index, vector in zip(sample_tracks, raw, strict=True):
            norm = float(np.linalg.norm(vector))
            if norm > 0:
                grouped.setdefault(track_index, []).append(vector / norm)
        results: list[SpeakerEmbedding] = []
        for track_index, vectors in grouped.items():
            centroid = np.mean(np.stack(vectors), axis=0)
            norm = float(np.linalg.norm(centroid))
            if norm == 0:
                continue
            centroid /= norm
            results.append(
                SpeakerEmbedding(
                    speaker_track_id=tracks[track_index].speaker_track_id,
                    model=self.model,
                    model_version=self.model_version,
                    vector=tuple(float(value) for value in centroid),
                    representatives=tuple(representatives.get(track_index, ())),
                    quality_score=min(1.0, durations.get(track_index, 0) / 12_000),
                )
            )
        return tuple(results)


@contextmanager
def _clip_directory(temp_root: Path | None):
    """Create an accessible private clip directory on Windows and POSIX.

    Python's Windows TemporaryDirectory creates a 0700 directory which can be
    inaccessible to its own process under the restricted desktop runtime.
    """
    base = (temp_root or Path(tempfile.gettempdir())).resolve()
    base.mkdir(parents=True, exist_ok=True)
    mode = 0o777 if os.name == "nt" else 0o700
    for _ in range(10):
        root = base / f"allday-v34-speakers-{uuid4().hex}"
        try:
            root.mkdir(mode=mode)
            break
        except FileExistsError:
            continue
    else:
        raise RuntimeError("could not allocate a speaker clip directory")
    try:
        yield root
    finally:
        if root.resolve().parent != base:
            raise RuntimeError("speaker clip directory escaped its temporary root")
        shutil.rmtree(root)


__all__ = ["FunASRSpeakerEmbeddingProvider"]
