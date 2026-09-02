from __future__ import annotations
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from allday_asr.v3.adapters.files import ContentAddressedStore
from allday_asr.v3.adapters.sqlite import V3Database

from .native_contracts import (
    NativeAsrSettings,
    NativeDiarizationSettings,
)
from .native_pipeline import NativeModelPipelineAdapter
from .native_runtime import _resolve_profile


def build_native_model_pipeline(
    database: V3Database,
    audio_store: ContentAddressedStore,
    config: Any,
    *,
    requested_profile: str | None = None,
    diarization_model_path: Path | None = None,
    builders: Mapping[str, Callable[..., Any]] | None = None,
) -> NativeModelPipelineAdapter:
    """Compose local model backends while keeping all execution state in V3."""

    if builders is None:
        from allday_asr.v3.adapters.models.asr_backends import (
            FunAsrNanoBackend,
            Qwen3AsrBackend,
            SpeechGateSettings,
        )
        from allday_asr.v3.adapters.models.diarization import PyannoteCommunityBackend

        builders = {
            "speech_gate": SpeechGateSettings,
            "primary": Qwen3AsrBackend,
            "secondary": FunAsrNanoBackend,
            "diarization": PyannoteCommunityBackend,
        }
    profile = requested_profile or config.asr.vram_profile
    profile = _resolve_profile(profile, device=config.runtime.device)
    batch_size = (
        config.asr.primary_batch_size_16gb
        if profile == "quality-16gb"
        else config.asr.primary_batch_size_8gb
    )
    gate = builders["speech_gate"](
        fsmn_merge_gap_ms=config.asr.speech_gate_fsmn_merge_gap_ms,
        max_utterance_ms=config.asr.speech_gate_max_utterance_ms,
        inference_padding_ms=config.asr.speech_gate_inference_padding_ms,
        speech_output_padding_ms=config.asr.speech_gate_output_padding_ms,
        min_candidate_ms=config.asr.speech_gate_min_candidate_ms,
        min_snr_db=config.asr.speech_gate_min_snr_db,
        silero_threshold=config.asr.speech_gate_silero_threshold,
        silero_min_speech_ms=config.asr.speech_gate_silero_min_speech_ms,
        silero_min_silence_ms=config.asr.speech_gate_silero_min_silence_ms,
        min_silero_overlap_ms=config.asr.speech_gate_min_silero_overlap_ms,
    )

    def primary() -> Any:
        return builders["primary"](
            model_id=config.asr.primary_model,
            aligner_model_id=config.asr.forced_aligner_model,
            device=config.runtime.device,
            batch_size=batch_size,
            max_new_tokens=config.asr.max_new_tokens,
            speech_gate=gate,
        )

    def secondary() -> Any:
        return builders["secondary"](
            model_id=config.asr.secondary_model,
            device=config.runtime.device,
        )

    def diarization() -> Any:
        quality = config.diarization
        selected_path = diarization_model_path or (
            Path(quality.model_path).resolve() if quality.model_path else None
        )
        return builders["diarization"](
            model_id=quality.model_id,
            model_path=selected_path,
            device=config.runtime.device,
            token_env=quality.token_env,
        )

    return NativeModelPipelineAdapter(
        database,
        audio_store,
        asr=NativeAsrSettings(
            language=config.asr.language,
            window_ms=round(config.asr.window_seconds * 1000),
            context_ms=round(config.asr.context_seconds * 1000),
        ),
        diarization=NativeDiarizationSettings(
            num_speakers=config.diarization.num_speakers,
            min_speakers=config.diarization.min_speakers,
            max_speakers=config.diarization.max_speakers,
            min_primary_overlap_ratio=config.diarization.min_primary_overlap_ratio,
        ),
        primary_factory=primary,
        secondary_factory=secondary,
        diarization_factory=diarization,
    )
