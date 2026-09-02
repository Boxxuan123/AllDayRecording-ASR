from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from allday_asr.v3.paths import DEFAULT_CONFIG_PATH


class ModelConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RuntimeModelConfig:
    device: str = "auto"


@dataclass(frozen=True)
class AsrModelConfig:
    language: str = "zh"
    primary_model: str = "Qwen/Qwen3-ASR-1.7B"
    forced_aligner_model: str = "Qwen/Qwen3-ForcedAligner-0.6B"
    secondary_model: str = "FunAudioLLM/Fun-ASR-Nano-2512"
    vram_profile: str = "auto"
    window_seconds: float = 300.0
    context_seconds: float = 5.0
    max_new_tokens: int = 4096
    primary_batch_size_16gb: int = 4
    primary_batch_size_8gb: int = 1
    speech_gate_fsmn_merge_gap_ms: int = 600
    speech_gate_max_utterance_ms: int = 30_000
    speech_gate_inference_padding_ms: int = 750
    speech_gate_output_padding_ms: int = 500
    speech_gate_min_candidate_ms: int = 800
    speech_gate_min_snr_db: float = 9.0
    speech_gate_silero_threshold: float = 0.15
    speech_gate_silero_min_speech_ms: int = 100
    speech_gate_silero_min_silence_ms: int = 250
    speech_gate_min_silero_overlap_ms: int = 500


@dataclass(frozen=True)
class DiarizationModelConfig:
    backend: str = "pyannote-community-1"
    model_id: str = "pyannote/speaker-diarization-community-1"
    model_path: str | None = None
    token_env: str = "HF_TOKEN"
    num_speakers: int | None = None
    min_speakers: int | None = None
    max_speakers: int | None = None
    min_primary_overlap_ratio: float = 0.50


@dataclass(frozen=True)
class ModelConfig:
    config_version: int = 1
    runtime: RuntimeModelConfig = RuntimeModelConfig()
    asr: AsrModelConfig = AsrModelConfig()
    diarization: DiarizationModelConfig = DiarizationModelConfig()


def load_model_config(path: Path | None = None) -> ModelConfig:
    config_path = (path or DEFAULT_CONFIG_PATH).resolve()
    if not config_path.is_file():
        raise ModelConfigError(f"模型配置文件不存在：{config_path}")
    try:
        with config_path.open("rb") as handle:
            payload = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ModelConfigError(f"TOML 模型配置格式错误：{exc}") from exc
    return model_config_from_mapping(payload)


def model_config_from_mapping(payload: dict[str, Any]) -> ModelConfig:
    _reject_unknown(payload, {"config_version", "runtime", "asr", "diarization"}, "root")
    version = int(payload.get("config_version", 1))
    if version != 1:
        raise ModelConfigError(f"不支持的模型配置版本：{version}")
    config = ModelConfig(
        config_version=version,
        runtime=RuntimeModelConfig(**_section(payload, "runtime", {"device"})),
        asr=AsrModelConfig(
            **_section(payload, "asr", set(AsrModelConfig.__dataclass_fields__))
        ),
        diarization=DiarizationModelConfig(
            **_section(
                payload,
                "diarization",
                set(DiarizationModelConfig.__dataclass_fields__),
            )
        ),
    )
    _validate(config)
    return config


def _section(
    payload: dict[str, Any], name: str, allowed_keys: set[str]
) -> dict[str, Any]:
    value = payload.get(name, {})
    if not isinstance(value, dict):
        raise ModelConfigError(f"[{name}] 必须是 TOML 表")
    _reject_unknown(value, allowed_keys, name)
    return value


def _reject_unknown(values: dict[str, Any], allowed: set[str], section: str) -> None:
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ModelConfigError(f"[{section}] 包含未知配置项：{', '.join(unknown)}")


def _validate(config: ModelConfig) -> None:
    if not config.runtime.device.strip():
        raise ModelConfigError("runtime.device 不能为空")
    asr = config.asr
    if not asr.language.strip():
        raise ModelConfigError("asr.language 不能为空")
    for field in ("primary_model", "forced_aligner_model", "secondary_model"):
        if not getattr(asr, field).strip():
            raise ModelConfigError(f"asr.{field} 不能为空")
    if asr.vram_profile not in {"auto", "quality-16gb", "compatible-8gb"}:
        raise ModelConfigError(
            "asr.vram_profile 必须是 auto、quality-16gb 或 compatible-8gb"
        )
    if asr.window_seconds <= 0 or asr.context_seconds < 0:
        raise ModelConfigError("ASR 窗口和上下文时长无效")
    if asr.max_new_tokens < 256:
        raise ModelConfigError("asr.max_new_tokens 不能小于 256")
    if asr.primary_batch_size_16gb < 1 or asr.primary_batch_size_8gb < 1:
        raise ModelConfigError("ASR batch size 必须大于 0")
    if not 0 < asr.speech_gate_silero_threshold < 1:
        raise ModelConfigError("Silero 门限必须在 0 到 1 之间")
    if min(
        asr.speech_gate_fsmn_merge_gap_ms,
        asr.speech_gate_inference_padding_ms,
        asr.speech_gate_output_padding_ms,
        asr.speech_gate_silero_min_silence_ms,
        asr.speech_gate_min_silero_overlap_ms,
    ) < 0:
        raise ModelConfigError("ASR speech gate 时长不能为负数")
    if min(
        asr.speech_gate_max_utterance_ms,
        asr.speech_gate_min_candidate_ms,
        asr.speech_gate_silero_min_speech_ms,
    ) < 1:
        raise ModelConfigError("ASR speech gate 时长必须大于 0")
    diarization = config.diarization
    if diarization.backend != "pyannote-community-1":
        raise ModelConfigError("diarization.backend 必须是 pyannote-community-1")
    if not diarization.model_id.strip() or not diarization.token_env.strip():
        raise ModelConfigError("说话人模型标识和 token 环境变量不能为空")
    for field in ("num_speakers", "min_speakers", "max_speakers"):
        value = getattr(diarization, field)
        if value is not None and value < 1:
            raise ModelConfigError(f"diarization.{field} 必须大于 0")
    if (
        diarization.num_speakers is None
        and diarization.min_speakers is not None
        and diarization.max_speakers is not None
        and diarization.min_speakers > diarization.max_speakers
    ):
        raise ModelConfigError("diarization.min_speakers 不能大于 max_speakers")
    if not 0 <= diarization.min_primary_overlap_ratio <= 1:
        raise ModelConfigError("diarization.min_primary_overlap_ratio 必须在 0 到 1 之间")


__all__ = [
    "AsrModelConfig",
    "DiarizationModelConfig",
    "ModelConfig",
    "ModelConfigError",
    "RuntimeModelConfig",
    "load_model_config",
    "model_config_from_mapping",
]
