from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from allday_asr.paths import DEFAULT_CONFIG_PATH


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RuntimeConfig:
    device: str = "auto"


@dataclass(frozen=True)
class IngestConfig:
    device: str = "Huawei Watch"
    timezone: str = "Asia/Singapore"


@dataclass(frozen=True)
class AsrConfig:
    language: str = "zh"
    primary_model: str = "Qwen/Qwen3-ASR-1.7B"
    forced_aligner_model: str = "Qwen/Qwen3-ForcedAligner-0.6B"
    secondary_model: str = "FunAudioLLM/Fun-ASR-Nano-2512"
    vram_profile: str = "quality-16gb"
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
class DiarizationConfig:
    enabled: bool = True
    preset_speakers: int | None = None
    min_segment_seconds: float = 1.5
    min_cluster_segments: int = 3
    min_cluster_speech_seconds: float = 5.0


@dataclass(frozen=True)
class IdentityConfig:
    candidates_enabled: bool = True
    threshold: float = 0.36
    min_segment_seconds: float = 0.8
    top: int = 30


@dataclass(frozen=True)
class TimelineConfig:
    max_gap_seconds: float = 120.0


@dataclass(frozen=True)
class ActionConfig:
    enabled: bool = True
    require_self_confirmation: bool = True
    confirmation_window_seconds: float = 90.0
    min_confidence: float = 0.75


@dataclass(frozen=True)
class AppConfig:
    config_version: int = 1
    runtime: RuntimeConfig = RuntimeConfig()
    ingest: IngestConfig = IngestConfig()
    asr: AsrConfig = AsrConfig()
    diarization: DiarizationConfig = DiarizationConfig()
    identity: IdentityConfig = IdentityConfig()
    timeline: TimelineConfig = TimelineConfig()
    actions: ActionConfig = ActionConfig()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def canonical_json(self) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def load_config(path: Path | None = None) -> AppConfig:
    config_path = (path or DEFAULT_CONFIG_PATH).resolve()
    if not config_path.is_file():
        raise ConfigError(f"配置文件不存在：{config_path}")
    try:
        with config_path.open("rb") as handle:
            payload = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"TOML 配置格式错误：{exc}") from exc
    return config_from_mapping(payload)


def config_from_mapping(payload: dict[str, Any]) -> AppConfig:
    _reject_unknown(
        payload,
        {
            "config_version",
            "runtime",
            "ingest",
            "asr",
            "diarization",
            "identity",
            "timeline",
            "actions",
        },
        "root",
    )
    version = int(payload.get("config_version", 1))
    if version != 1:
        raise ConfigError(f"不支持的配置版本：{version}")

    runtime_values = _section(payload, "runtime", {"device"})
    ingest_values = _section(payload, "ingest", {"device", "timezone"})
    asr_values = _section(
        payload,
        "asr",
        {
            "language",
            "primary_model",
            "forced_aligner_model",
            "secondary_model",
            "vram_profile",
            "window_seconds",
            "context_seconds",
            "max_new_tokens",
            "primary_batch_size_16gb",
            "primary_batch_size_8gb",
            "speech_gate_fsmn_merge_gap_ms",
            "speech_gate_max_utterance_ms",
            "speech_gate_inference_padding_ms",
            "speech_gate_output_padding_ms",
            "speech_gate_min_candidate_ms",
            "speech_gate_min_snr_db",
            "speech_gate_silero_threshold",
            "speech_gate_silero_min_speech_ms",
            "speech_gate_silero_min_silence_ms",
            "speech_gate_min_silero_overlap_ms",
        },
    )
    diarization_values = _section(
        payload,
        "diarization",
        {
            "enabled",
            "preset_speakers",
            "min_segment_seconds",
            "min_cluster_segments",
            "min_cluster_speech_seconds",
        },
    )
    identity_values = _section(
        payload,
        "identity",
        {"candidates_enabled", "threshold", "min_segment_seconds", "top"},
    )
    timeline_values = _section(payload, "timeline", {"max_gap_seconds"})
    action_values = _section(
        payload,
        "actions",
        {
            "enabled",
            "require_self_confirmation",
            "confirmation_window_seconds",
            "min_confidence",
        },
    )

    config = AppConfig(
        config_version=version,
        runtime=RuntimeConfig(**runtime_values),
        ingest=IngestConfig(**ingest_values),
        asr=AsrConfig(**asr_values),
        diarization=DiarizationConfig(**diarization_values),
        identity=IdentityConfig(**identity_values),
        timeline=TimelineConfig(**timeline_values),
        actions=ActionConfig(**action_values),
    )
    _validate(config)
    return config


def _section(
    payload: dict[str, Any], name: str, allowed_keys: set[str]
) -> dict[str, Any]:
    value = payload.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] 必须是 TOML 表")
    _reject_unknown(value, allowed_keys, name)
    return value


def _reject_unknown(values: dict[str, Any], allowed: set[str], section: str) -> None:
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ConfigError(f"[{section}] 包含未知配置项：{', '.join(unknown)}")


def _validate(config: AppConfig) -> None:
    if not config.runtime.device.strip():
        raise ConfigError("runtime.device 不能为空")
    if not config.ingest.device.strip():
        raise ConfigError("ingest.device 不能为空")
    try:
        ZoneInfo(config.ingest.timezone)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"未知时区：{config.ingest.timezone}") from exc
    if not config.asr.language.strip():
        raise ConfigError("asr.language 不能为空")
    asr = config.asr
    for field in ("primary_model", "forced_aligner_model", "secondary_model"):
        if not getattr(asr, field).strip():
            raise ConfigError(f"asr.{field} 不能为空")
    if asr.vram_profile not in {"quality-16gb", "compatible-8gb"}:
        raise ConfigError("asr.vram_profile 必须是 quality-16gb 或 compatible-8gb")
    if asr.window_seconds <= 0:
        raise ConfigError("asr.window_seconds 必须大于 0")
    if asr.context_seconds < 0:
        raise ConfigError("asr.context_seconds 不能小于 0")
    if asr.max_new_tokens < 256:
        raise ConfigError("asr.max_new_tokens 不能小于 256")
    if asr.primary_batch_size_16gb < 1 or asr.primary_batch_size_8gb < 1:
        raise ConfigError("ASR batch size 必须大于 0")
    if asr.speech_gate_fsmn_merge_gap_ms < 0:
        raise ConfigError("asr.speech_gate_fsmn_merge_gap_ms 不能小于 0")
    if asr.speech_gate_max_utterance_ms < 1:
        raise ConfigError("asr.speech_gate_max_utterance_ms 必须大于 0")
    if (
        asr.speech_gate_inference_padding_ms < 0
        or asr.speech_gate_output_padding_ms < 0
    ):
        raise ConfigError("ASR speech gate padding 不能小于 0")
    if asr.speech_gate_min_candidate_ms < 1:
        raise ConfigError("asr.speech_gate_min_candidate_ms 必须大于 0")
    if not 0 < asr.speech_gate_silero_threshold < 1:
        raise ConfigError("asr.speech_gate_silero_threshold 必须在 0 到 1 之间")
    if (
        asr.speech_gate_silero_min_speech_ms < 1
        or asr.speech_gate_silero_min_silence_ms < 0
        or asr.speech_gate_min_silero_overlap_ms < 0
    ):
        raise ConfigError("ASR Silero speech gate 时长配置无效")
    diarization = config.diarization
    if diarization.preset_speakers is not None and diarization.preset_speakers < 1:
        raise ConfigError("diarization.preset_speakers 必须大于 0")
    if diarization.min_segment_seconds < 0.5:
        raise ConfigError("diarization.min_segment_seconds 不能小于 0.5")
    if diarization.min_cluster_segments < 1:
        raise ConfigError("diarization.min_cluster_segments 必须大于 0")
    if diarization.min_cluster_speech_seconds < 0:
        raise ConfigError("diarization.min_cluster_speech_seconds 不能小于 0")
    identity = config.identity
    if not 0 < identity.threshold <= 1:
        raise ConfigError("identity.threshold 必须在 0 到 1 之间")
    if identity.min_segment_seconds < 0.8:
        raise ConfigError("identity.min_segment_seconds 不能小于 0.8")
    if not 1 <= identity.top <= 100:
        raise ConfigError("identity.top 必须在 1 到 100 之间")
    if config.timeline.max_gap_seconds <= 0:
        raise ConfigError("timeline.max_gap_seconds 必须大于 0")
    if config.actions.confirmation_window_seconds <= 0:
        raise ConfigError("actions.confirmation_window_seconds 必须大于 0")
    if not 0 < config.actions.min_confidence <= 1:
        raise ConfigError("actions.min_confidence 必须在 0 到 1 之间")
