from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from allday_asr.application.diarization.base import QualityDiarizationSettings
from allday_asr.config import AppConfig
from allday_asr.services.quality_asr import QualityAsrSettings
from allday_asr.services.runtime_profile import resolve_vram_profile

BackendBuilder = Callable[..., object]
BackendFactory = Callable[[], object]
ProfileResolver = Callable[..., str]


@dataclass(frozen=True)
class RuntimeBackendBuilders:
    speech_gate: BackendBuilder
    primary_asr: BackendBuilder
    secondary_asr: BackendBuilder
    diarization: BackendBuilder


@dataclass(frozen=True)
class QualityAsrRuntime:
    settings: QualityAsrSettings
    primary_factory: BackendFactory
    secondary_factory: BackendFactory


@dataclass(frozen=True)
class QualityDiarizationRuntime:
    settings: QualityDiarizationSettings
    backend_factory: BackendFactory


def default_backend_builders() -> RuntimeBackendBuilders:
    """Load concrete backend classes only when a model command needs them."""
    from allday_asr.asr.quality_backends import (
        FunAsrNanoBackend,
        Qwen3AsrBackend,
        SpeechGateSettings,
    )
    from allday_asr.diarization.quality_backends import PyannoteCommunityBackend

    return RuntimeBackendBuilders(
        speech_gate=SpeechGateSettings,
        primary_asr=Qwen3AsrBackend,
        secondary_asr=FunAsrNanoBackend,
        diarization=PyannoteCommunityBackend,
    )


def build_oracle_backend(model: str, *, device: str) -> object:
    from allday_asr.asr.oracle_backends import create_oracle_backend

    return create_oracle_backend(model, device=device)


def runtime_signature(values: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        values, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_quality_asr_runtime(
    resolved: AppConfig,
    *,
    requested_profile: str | None,
    max_windows: int | None = None,
    builders: RuntimeBackendBuilders | None = None,
    profile_resolver: ProfileResolver = resolve_vram_profile,
) -> QualityAsrRuntime:
    selected_profile = profile_resolver(
        requested_profile or resolved.asr.vram_profile,
        device=resolved.runtime.device,
    )
    batch_size = (
        resolved.asr.primary_batch_size_16gb
        if selected_profile == "quality-16gb"
        else resolved.asr.primary_batch_size_8gb
    )
    model_signature = runtime_signature(
        {
            "primary_model": resolved.asr.primary_model,
            "forced_aligner_model": resolved.asr.forced_aligner_model,
            "secondary_model": resolved.asr.secondary_model,
            "device": resolved.runtime.device,
            "profile": selected_profile,
            "primary_batch_size": batch_size,
            "max_new_tokens": resolved.asr.max_new_tokens,
            "precision": {"primary": "bfloat16", "secondary": "bf16"},
        }
    )
    settings = QualityAsrSettings(
        language=resolved.asr.language,
        window_ms=round(resolved.asr.window_seconds * 1000),
        context_ms=round(resolved.asr.context_seconds * 1000),
        vram_profile=selected_profile,
        model_signature=model_signature,
        max_windows=max_windows,
        speech_gate_fsmn_merge_gap_ms=resolved.asr.speech_gate_fsmn_merge_gap_ms,
        speech_gate_max_utterance_ms=resolved.asr.speech_gate_max_utterance_ms,
        speech_gate_inference_padding_ms=(
            resolved.asr.speech_gate_inference_padding_ms
        ),
        speech_gate_output_padding_ms=resolved.asr.speech_gate_output_padding_ms,
        speech_gate_min_candidate_ms=resolved.asr.speech_gate_min_candidate_ms,
        speech_gate_min_snr_db=resolved.asr.speech_gate_min_snr_db,
        speech_gate_silero_threshold=resolved.asr.speech_gate_silero_threshold,
        speech_gate_silero_min_speech_ms=(
            resolved.asr.speech_gate_silero_min_speech_ms
        ),
        speech_gate_silero_min_silence_ms=(
            resolved.asr.speech_gate_silero_min_silence_ms
        ),
        speech_gate_min_silero_overlap_ms=(
            resolved.asr.speech_gate_min_silero_overlap_ms
        ),
    )
    selected_builders = builders or default_backend_builders()
    speech_gate = selected_builders.speech_gate(
        fsmn_merge_gap_ms=settings.speech_gate_fsmn_merge_gap_ms,
        max_utterance_ms=settings.speech_gate_max_utterance_ms,
        inference_padding_ms=settings.speech_gate_inference_padding_ms,
        speech_output_padding_ms=settings.speech_gate_output_padding_ms,
        min_candidate_ms=settings.speech_gate_min_candidate_ms,
        min_snr_db=settings.speech_gate_min_snr_db,
        silero_threshold=settings.speech_gate_silero_threshold,
        silero_min_speech_ms=settings.speech_gate_silero_min_speech_ms,
        silero_min_silence_ms=settings.speech_gate_silero_min_silence_ms,
        min_silero_overlap_ms=settings.speech_gate_min_silero_overlap_ms,
    )

    def primary_factory() -> object:
        return selected_builders.primary_asr(
            model_id=resolved.asr.primary_model,
            aligner_model_id=resolved.asr.forced_aligner_model,
            device=resolved.runtime.device,
            batch_size=batch_size,
            max_new_tokens=resolved.asr.max_new_tokens,
            speech_gate=speech_gate,
        )

    def secondary_factory() -> object:
        return selected_builders.secondary_asr(
            model_id=resolved.asr.secondary_model,
            device=resolved.runtime.device,
        )

    return QualityAsrRuntime(
        settings=settings,
        primary_factory=primary_factory,
        secondary_factory=secondary_factory,
    )


def build_quality_diarization_runtime(
    resolved: AppConfig,
    *,
    model_path: Path | None,
    num_speakers: int | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    builders: RuntimeBackendBuilders | None = None,
) -> QualityDiarizationRuntime:
    quality = resolved.quality_diarization
    selected_model_path = model_path or (
        Path(quality.model_path).resolve() if quality.model_path else None
    )
    model_signature = runtime_signature(
        {
            "backend": quality.backend,
            "model_id": quality.model_id,
            "model_path": str(selected_model_path) if selected_model_path else None,
            "device": resolved.runtime.device,
        }
    )
    settings = QualityDiarizationSettings(
        num_speakers=num_speakers if num_speakers is not None else quality.num_speakers,
        min_speakers=min_speakers if min_speakers is not None else quality.min_speakers,
        max_speakers=max_speakers if max_speakers is not None else quality.max_speakers,
        min_primary_overlap_ratio=quality.min_primary_overlap_ratio,
        min_secondary_overlap_ratio=quality.min_secondary_overlap_ratio,
        min_primary_margin=quality.min_primary_margin,
        model_signature=model_signature,
    )
    selected_builders = builders or default_backend_builders()

    def backend_factory() -> object:
        return selected_builders.diarization(
            model_id=quality.model_id,
            model_path=selected_model_path,
            device=resolved.runtime.device,
            token_env=quality.token_env,
        )

    return QualityDiarizationRuntime(
        settings=settings,
        backend_factory=backend_factory,
    )


__all__ = [
    "QualityAsrRuntime",
    "QualityDiarizationRuntime",
    "RuntimeBackendBuilders",
    "build_oracle_backend",
    "build_quality_asr_runtime",
    "build_quality_diarization_runtime",
    "default_backend_builders",
    "runtime_signature",
]
