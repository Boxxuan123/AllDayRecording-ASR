from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

QWEN_ASR_MODEL_ID = "Qwen/Qwen3-ASR-1.7B"


QWEN_ALIGNER_MODEL_ID = "Qwen/Qwen3-ForcedAligner-0.6B"


FUN_ASR_MODEL_ID = "FunAudioLLM/Fun-ASR-Nano-2512"


@dataclass(frozen=True)
class AlignedToken:
    text: str
    start_seconds: float
    end_seconds: float
    confidence: float | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class HypothesisResult:
    text: str
    language: str | None
    tokens: tuple[AlignedToken, ...]
    raw_response: dict[str, Any]


@dataclass(frozen=True)
class SpeechGateSettings:
    fsmn_merge_gap_ms: int = 600
    max_utterance_ms: int = 30_000
    inference_padding_ms: int = 750
    speech_output_padding_ms: int = 500
    min_candidate_ms: int = 800
    min_snr_db: float = 9.0
    silero_threshold: float = 0.15
    silero_min_speech_ms: int = 100
    silero_min_silence_ms: int = 250
    min_silero_overlap_ms: int = 500

    def __post_init__(self) -> None:
        if self.fsmn_merge_gap_ms < 0:
            raise ValueError("fsmn_merge_gap_ms cannot be negative")
        if self.max_utterance_ms < 1:
            raise ValueError("max_utterance_ms must be positive")
        if self.inference_padding_ms < 0 or self.speech_output_padding_ms < 0:
            raise ValueError("speech gate padding cannot be negative")
        if self.min_candidate_ms < 1:
            raise ValueError("min_candidate_ms must be positive")
        if not 0 < self.silero_threshold < 1:
            raise ValueError("silero_threshold must be between zero and one")
        if self.silero_min_speech_ms < 1 or self.silero_min_silence_ms < 0:
            raise ValueError("Silero duration settings are invalid")
        if self.min_silero_overlap_ms < 0:
            raise ValueError("min_silero_overlap_ms cannot be negative")


@dataclass(frozen=True)
class SpeechGateCandidate:
    candidate_index: int
    core_start_ms: int
    core_end_ms: int
    inference_start_ms: int
    inference_end_ms: int
    duration_ms: int
    rms_dbfs: float
    noise_floor_dbfs: float
    snr_db: float
    silero_overlap_ms: int
    accepted: bool
    acceptance_reasons: tuple[str, ...]


class QualityAsrBackend(Protocol):
    role: str
    model_id: str
    alignment_model_id: str | None
    backend_name: str
    model_revision: str | None

    def transcribe(
        self, audio_path: Path, *, language: str | None
    ) -> HypothesisResult: ...

    def parameters(self) -> dict[str, Any]: ...

    def close(self) -> None: ...
