from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from allday_asr.asr.funasr_backend import (
    ASR_MODEL_ID,
    FunASRBackend,
    resolve_device,
)
from allday_asr.asr.quality_backends import (
    FUN_ASR_MODEL_ID,
    QWEN_ASR_MODEL_ID,
    _cached_huggingface_or_id,
    _cached_modelscope_or_id,
    _funasr_language,
    _json_safe,
    _qwen_language,
    _release_cuda,
)
from allday_asr.paths import AppPaths, DEFAULT_PATHS, configure_model_cache


@dataclass(frozen=True)
class OracleTranscript:
    text: str
    language: str | None
    raw_response: dict[str, Any]


class OracleAsrBackend(Protocol):
    model_id: str
    backend_name: str
    model_revision: str | None

    def transcribe(self, audio_path: Path, *, language: str | None) -> OracleTranscript:
        ...

    def parameters(self) -> dict[str, Any]:
        ...

    def close(self) -> None:
        ...


class SenseVoiceOracleBackend:
    """SenseVoice without VAD, run on a human-supplied reference interval."""

    model_id = ASR_MODEL_ID
    backend_name = "funasr-sensevoice-oracle-boundary"
    model_revision: str | None = None

    def __init__(
        self, *, device: str = "auto", paths: AppPaths | None = None
    ):
        self._backend = FunASRBackend(device=device, paths=paths)

    def transcribe(self, audio_path: Path, *, language: str | None) -> OracleTranscript:
        import soundfile as sf

        samples, _ = sf.read(
            str(audio_path.resolve(strict=True)), dtype="float32", always_2d=False
        )
        if getattr(samples, "ndim", 1) > 1:
            samples = samples.mean(axis=1)
        result = self._backend.transcribe(samples, language=language or "zh")
        return OracleTranscript(
            text=result.display_text,
            language=result.language,
            raw_response={"raw_text": result.raw_text},
        )

    def parameters(self) -> dict[str, Any]:
        return {
            "device": self._backend.device,
            "use_itn": True,
            "segmentation": "human-reference-interval",
            "package": "funasr",
            "package_version": self._backend.package_version,
        }

    def close(self) -> None:
        self._backend = None  # type: ignore[assignment]
        _release_cuda()


class QwenOracleBackend:
    """Qwen3-ASR without FSMN-VAD or ForcedAligner boundary effects."""

    backend_name = "qwen-asr-oracle-boundary"

    def __init__(
        self,
        *,
        model_id: str = QWEN_ASR_MODEL_ID,
        device: str = "auto",
        dtype: str = "bfloat16",
        max_new_tokens: int = 4096,
        paths: AppPaths | None = None,
    ):
        self.paths = paths or DEFAULT_PATHS
        self.model_id = model_id
        self.device = resolve_device(device)
        self.dtype_name = dtype
        self.max_new_tokens = max_new_tokens
        self.model_revision: str | None = None
        self._model = None

    def ensure_loaded(self) -> None:
        if self._model is not None:
            return
        configure_model_cache(self.paths)
        import torch
        from qwen_asr import Qwen3ASRModel

        dtype = getattr(torch, self.dtype_name)
        self._model = Qwen3ASRModel.from_pretrained(
            _cached_huggingface_or_id(
                self.model_id, model_dir=self.paths.model_dir
            ),
            dtype=dtype,
            device_map=self.device,
            max_inference_batch_size=1,
            max_new_tokens=self.max_new_tokens,
        )
        config = getattr(getattr(self._model, "model", None), "config", None)
        self.model_revision = getattr(config, "_commit_hash", None)

    def transcribe(self, audio_path: Path, *, language: str | None) -> OracleTranscript:
        self.ensure_loaded()
        response = self._model.transcribe(
            audio=str(audio_path.resolve(strict=True)),
            language=_qwen_language(language),
        )
        item = response[0] if response else None
        return OracleTranscript(
            text=str(getattr(item, "text", "") or "").strip(),
            language=str(getattr(item, "language", "") or "") or language,
            raw_response={
                "text": str(getattr(item, "text", "") or ""),
                "language": str(getattr(item, "language", "") or ""),
            },
        )

    def parameters(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "dtype": self.dtype_name,
            "max_inference_batch_size": 1,
            "max_new_tokens": self.max_new_tokens,
            "forced_aligner": None,
            "vad_model": None,
            "segmentation": "human-reference-interval",
            "package": "qwen-asr",
            "package_version": importlib.metadata.version("qwen-asr"),
        }

    def close(self) -> None:
        self._model = None
        _release_cuda()


class FunAsrNanoOracleBackend:
    """Fun-ASR-Nano without its optional VAD wrapper."""

    backend_name = "funasr-nano-oracle-boundary"

    def __init__(
        self,
        *,
        model_id: str = FUN_ASR_MODEL_ID,
        device: str = "auto",
        dtype: str = "bf16",
        paths: AppPaths | None = None,
    ):
        self.paths = paths or DEFAULT_PATHS
        self.model_id = model_id
        self.device = resolve_device(device)
        self.dtype_name = dtype
        self.model_revision: str | None = None
        self._model = None

    def ensure_loaded(self) -> None:
        if self._model is not None:
            return
        configure_model_cache(self.paths)
        from funasr import AutoModel

        self._model = AutoModel(
            model=_cached_modelscope_or_id(
                self.model_id, model_dir=self.paths.model_dir
            ),
            device=self.device,
            dtype=self.dtype_name,
            trust_remote_code=True,
            disable_update=True,
            disable_pbar=True,
        )

    def transcribe(self, audio_path: Path, *, language: str | None) -> OracleTranscript:
        self.ensure_loaded()
        response = self._model.generate(
            input=str(audio_path.resolve(strict=True)),
            cache={},
            batch_size=1,
            language=_funasr_language(language),
        )
        item = dict(response[0]) if response else {}
        return OracleTranscript(
            text=str(item.get("text", "")).strip(),
            language=language,
            raw_response=_json_safe(item),
        )

    def parameters(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "dtype": self.dtype_name,
            "batch_size": 1,
            "vad_model": None,
            "segmentation": "human-reference-interval",
            "package": "funasr",
            "package_version": importlib.metadata.version("funasr"),
        }

    def close(self) -> None:
        self._model = None
        _release_cuda()


def create_oracle_backend(
    name: str, *, device: str = "auto", paths: AppPaths | None = None
) -> OracleAsrBackend:
    normalized = name.strip().lower()
    if normalized == "sensevoice":
        return SenseVoiceOracleBackend(device=device, paths=paths)
    if normalized == "qwen":
        return QwenOracleBackend(device=device, paths=paths)
    if normalized in {"fun", "funasr", "fun-asr-nano"}:
        return FunAsrNanoOracleBackend(device=device, paths=paths)
    raise ValueError("model 必须是 sensevoice、qwen 或 fun")
