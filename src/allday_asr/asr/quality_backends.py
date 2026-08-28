from __future__ import annotations

import importlib.metadata
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from allday_asr.asr.funasr_backend import _cached_model_or_id, resolve_device
from allday_asr.paths import MODEL_DIR, configure_model_cache


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


class QualityAsrBackend(Protocol):
    role: str
    model_id: str
    alignment_model_id: str | None
    backend_name: str
    model_revision: str | None

    def transcribe(self, audio_path: Path, *, language: str | None) -> HypothesisResult:
        ...

    def parameters(self) -> dict[str, Any]:
        ...

    def close(self) -> None:
        ...


class Qwen3AsrBackend:
    role = "primary"
    backend_name = "qwen-asr-transformers"

    def __init__(
        self,
        *,
        model_id: str = QWEN_ASR_MODEL_ID,
        aligner_model_id: str = QWEN_ALIGNER_MODEL_ID,
        device: str = "auto",
        batch_size: int = 1,
        max_new_tokens: int = 4096,
        dtype: str = "bfloat16",
    ):
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        configure_model_cache()
        self.model_id = model_id
        self.alignment_model_id = aligner_model_id
        self.device = resolve_device(device)
        self.batch_size = batch_size
        self.max_new_tokens = max_new_tokens
        self.dtype_name = dtype
        self.model_revision: str | None = None
        self._model = None
        self._vad_model = None

    def ensure_loaded(self) -> None:
        if self._model is not None:
            return
        import torch
        from qwen_asr import Qwen3ASRModel

        dtype = getattr(torch, self.dtype_name)
        model_path = _cached_huggingface_or_id(self.model_id)
        aligner_path = _cached_huggingface_or_id(self.alignment_model_id)
        self._model = Qwen3ASRModel.from_pretrained(
            model_path,
            dtype=dtype,
            device_map=self.device,
            max_inference_batch_size=self.batch_size,
            max_new_tokens=self.max_new_tokens,
            forced_aligner=aligner_path,
            forced_aligner_kwargs={"dtype": dtype, "device_map": self.device},
        )
        config = getattr(getattr(self._model, "model", None), "config", None)
        self.model_revision = getattr(config, "_commit_hash", None)

    def transcribe(self, audio_path: Path, *, language: str | None) -> HypothesisResult:
        import soundfile as sf

        waveform, sample_rate = sf.read(
            str(audio_path.resolve(strict=True)), dtype="float32", always_2d=False
        )
        if getattr(waveform, "ndim", 1) > 1:
            waveform = waveform.mean(axis=1)
        speech_ranges = self._detect_speech(audio_path, len(waveform), sample_rate)
        if not speech_ranges:
            return HypothesisResult(
                text="",
                language=None,
                tokens=(),
                raw_response={"speech_ranges_ms": [], "segments": []},
            )
        self.ensure_loaded()
        qwen_language = _qwen_language(language)
        audio_segments = [
            (
                waveform[
                    round(start_ms * sample_rate / 1000) :
                    round(end_ms * sample_rate / 1000)
                ],
                sample_rate,
            )
            for start_ms, end_ms in speech_ranges
        ]
        results = self._model.transcribe(
            audio=audio_segments,
            language=[qwen_language] * len(audio_segments),
            return_time_stamps=True,
        )
        tokens: list[AlignedToken] = []
        texts: list[str] = []
        languages: list[str] = []
        raw_segments: list[dict[str, Any]] = []
        for (start_ms, end_ms), result in zip(speech_ranges, results, strict=True):
            text = str(result.text or "").strip()
            result_language = str(result.language or "")
            if text:
                texts.append(text)
            if result_language and result_language not in languages:
                languages.append(result_language)
            items = list(getattr(result.time_stamps, "items", []) or [])
            for item in items:
                start_seconds = start_ms / 1000 + float(item.start_time)
                end_seconds = start_ms / 1000 + float(item.end_time)
                if end_seconds <= start_seconds:
                    continue
                tokens.append(
                    AlignedToken(
                        text=str(item.text),
                        start_seconds=start_seconds,
                        end_seconds=end_seconds,
                        metadata={
                            "timestamp_source": self.alignment_model_id,
                            "utterance_start_ms": start_ms,
                            "utterance_end_ms": end_ms,
                        },
                    )
                )
            raw_segments.append(
                {
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "language": result_language,
                    "text": text,
                    "aligned_token_count": len(items),
                }
            )
        merged_text = "".join(texts)
        raw = {
            "language": ",".join(languages),
            "text": merged_text,
            "speech_ranges_ms": speech_ranges,
            "segments": raw_segments,
            "time_stamps": [asdict(token) for token in tokens],
        }
        return HypothesisResult(
            text=merged_text,
            language=",".join(languages) or None,
            tokens=tuple(tokens),
            raw_response=raw,
        )

    def _detect_speech(
        self, audio_path: Path, sample_count: int, sample_rate: int
    ) -> list[tuple[int, int]]:
        from funasr import AutoModel

        if self._vad_model is None:
            self._vad_model = AutoModel(
                model=_cached_model_or_id("fsmn-vad"),
                device="cpu",
                disable_update=True,
                disable_pbar=True,
            )
        response = self._vad_model.generate(
            input=str(audio_path.resolve(strict=True)),
            batch_size=1,
            max_single_segment_time=30_000,
        )
        values = response[0].get("value", []) if response else []
        raw = [
            (max(0, int(value[0])), int(value[1]))
            for value in values
            if isinstance(value, (list, tuple))
            and len(value) >= 2
            and int(value[1]) > int(value[0])
        ]
        duration_ms = round(sample_count * 1000 / sample_rate)
        return _padded_non_overlapping_ranges(raw, duration_ms, padding_ms=750)

    def parameters(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "dtype": self.dtype_name,
            "max_inference_batch_size": self.batch_size,
            "max_new_tokens": self.max_new_tokens,
            "return_time_stamps": True,
            "segmentation": "fsmn-vad-30s-padding750ms-nonoverlap-v1",
            "vad_device": "cpu",
            "package": "qwen-asr",
            "package_version": importlib.metadata.version("qwen-asr"),
        }

    def close(self) -> None:
        self._model = None
        self._vad_model = None
        _release_cuda()


class FunAsrNanoBackend:
    role = "secondary"
    backend_name = "funasr-pytorch"

    def __init__(
        self,
        *,
        model_id: str = FUN_ASR_MODEL_ID,
        device: str = "auto",
        dtype: str = "bf16",
    ):
        configure_model_cache()
        self.model_id = model_id
        self.alignment_model_id = f"{model_id}:ctc"
        self.device = resolve_device(device)
        self.dtype_name = dtype
        self.model_revision: str | None = None
        self._model = None

    def ensure_loaded(self) -> None:
        if self._model is not None:
            return
        from funasr import AutoModel

        model_path = _cached_modelscope_or_id(self.model_id)
        self._model = AutoModel(
            model=model_path,
            device=self.device,
            dtype=self.dtype_name,
            trust_remote_code=True,
            vad_model=_cached_model_or_id("fsmn-vad"),
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


def _qwen_language(language: str | None) -> str | None:
    if not language or language.lower() == "auto":
        return None
    return {
        "zh": "Chinese",
        "yue": "Cantonese",
        "en": "English",
        "ja": "Japanese",
        "ko": "Korean",
    }.get(language.lower(), language)


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


def _json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return {"repr": repr(value)}


def _cached_modelscope_or_id(model_id: str) -> str:
    root = MODEL_DIR / "modelscope" / "models" / model_id.replace("/", "--") / "snapshots"
    if root.is_dir():
        snapshots = sorted(
            (path for path in root.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if snapshots:
            return str(snapshots[0])
    return model_id


def _cached_huggingface_or_id(model_id: str) -> str:
    root = MODEL_DIR / "huggingface" / "hub" / f"models--{model_id.replace('/', '--')}" / "snapshots"
    if root.is_dir():
        snapshots = sorted(
            (path for path in root.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if snapshots:
            return str(snapshots[0])
    modelscope_path = _cached_modelscope_or_id(model_id)
    if modelscope_path != model_id:
        return modelscope_path
    return model_id


def _release_cuda() -> None:
    import gc

    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _padded_non_overlapping_ranges(
    ranges: list[tuple[int, int]], duration_ms: int, *, padding_ms: int
) -> list[tuple[int, int]]:
    if not ranges:
        return []
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        start = max(0, min(duration_ms, start))
        end = max(0, min(duration_ms, end))
        if end <= start:
            continue
        if merged and start - merged[-1][1] <= 600 and end - merged[-1][0] <= 30_000:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    padded = [
        [max(0, start - padding_ms), min(duration_ms, end + padding_ms)]
        for start, end in merged
    ]
    for index in range(len(padded) - 1):
        if padded[index][1] <= padded[index + 1][0]:
            continue
        boundary = round((merged[index][1] + merged[index + 1][0]) / 2)
        padded[index][1] = boundary
        padded[index + 1][0] = boundary
    return [(start, end) for start, end in padded if end > start]
