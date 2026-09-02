from __future__ import annotations
import importlib.metadata
from pathlib import Path
from typing import Any
from allday_asr.v3.adapters.models.funasr import _cached_model_or_id, resolve_device
from allday_asr.v3.paths import (
    DEFAULT_MODEL_PATHS,
    ModelPaths,
    configure_model_cache,
)

from .asr_types import (
    AlignedToken,
    FUN_ASR_MODEL_ID,
    HypothesisResult,
)
from .asr_model_cache import (
    _cached_modelscope_or_id,
    _json_safe,
    _release_cuda,
)


class FunAsrNanoBackend:
    role = "secondary"
    backend_name = "funasr-pytorch"

    def __init__(
        self,
        *,
        model_id: str = FUN_ASR_MODEL_ID,
        device: str = "auto",
        dtype: str = "bf16",
        paths: ModelPaths | None = None,
    ):
        self.paths = paths or DEFAULT_MODEL_PATHS
        self.model_id = model_id
        self.alignment_model_id = f"{model_id}:ctc"
        self.device = resolve_device(device)
        self.dtype_name = dtype
        self.model_revision: str | None = None
        self._model = None

    def ensure_loaded(self) -> None:
        if self._model is not None:
            return
        configure_model_cache(self.paths)
        from funasr import AutoModel

        model_path = _cached_modelscope_or_id(
            self.model_id, model_dir=self.paths.model_dir
        )
        self._model = AutoModel(
            model=model_path,
            device=self.device,
            dtype=self.dtype_name,
            trust_remote_code=True,
            vad_model=_cached_model_or_id("fsmn-vad", model_dir=self.paths.model_dir),
            vad_kwargs={"max_single_segment_time": 30_000},
            disable_update=True,
            disable_pbar=True,
        )

    def transcribe(self, audio_path: Path, *, language: str | None) -> HypothesisResult:
        self.ensure_loaded()
        response = self._model.generate(
            input=str(audio_path.resolve(strict=True)),
            cache={},
            batch_size=1,
            language=_funasr_language(language),
        )
        item = dict(response[0]) if response else {}
        raw_timestamps = item.get("timestamps") or item.get("timestamp") or []
        tokens: list[AlignedToken] = []
        for index, value in enumerate(raw_timestamps):
            token = _funasr_timestamp(value, index)
            if token is not None:
                tokens.append(token)
        return HypothesisResult(
            text=str(item.get("text", "")).strip(),
            language=language,
            tokens=tuple(tokens),
            raw_response=_json_safe(item),
        )

    def parameters(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "dtype": self.dtype_name,
            "batch_size": 1,
            "vad_model": "fsmn-vad",
            "vad_max_single_segment_ms": 30_000,
            "package": "funasr",
            "package_version": importlib.metadata.version("funasr"),
        }

    def close(self) -> None:
        self._model = None
        _release_cuda()


def _funasr_language(language: str | None) -> str:
    if not language or language.lower() == "auto":
        return "auto"
    return {"zh": "中文", "en": "English", "ja": "日本語"}.get(
        language.lower(), language
    )


def _funasr_timestamp(value: Any, index: int) -> AlignedToken | None:
    if isinstance(value, dict):
        start = float(value.get("start_time", value.get("start", 0)))
        end = float(value.get("end_time", value.get("end", 0)))
        text = str(value.get("token", value.get("text", "")))
        if end <= start:
            return None
        return AlignedToken(
            text=text,
            start_seconds=start,
            end_seconds=end,
            metadata={"timestamp_source": "ctc", "raw_index": index},
        )
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        start = float(value[-2]) / 1000.0
        end = float(value[-1]) / 1000.0
        text = str(value[0]) if len(value) >= 3 else ""
        if end <= start:
            return None
        return AlignedToken(
            text=text,
            start_seconds=start,
            end_seconds=end,
            metadata={"timestamp_source": "ctc", "raw_index": index},
        )
    return None
