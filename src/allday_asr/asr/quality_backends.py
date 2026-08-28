from __future__ import annotations

import importlib.metadata
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

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
        speech_gate: SpeechGateSettings | None = None,
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
        self.speech_gate = speech_gate or SpeechGateSettings()
        self.model_revision: str | None = None
        self._model = None
        self._vad_model = None
        self._silero_model = None

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
        duration_ms = round(len(waveform) * 1000 / sample_rate)
        fsmn_ranges = self._detect_fsmn_speech(audio_path, duration_ms)
        silero_ranges = self._detect_silero_speech(waveform, sample_rate)
        candidates, speech_ranges = _build_speech_gate(
            waveform,
            sample_rate,
            fsmn_ranges,
            silero_ranges,
            settings=self.speech_gate,
        )
        if not candidates:
            return HypothesisResult(
                text="",
                language=None,
                tokens=(),
                raw_response={
                    "speech_ranges_ms": [],
                    "candidate_ranges_ms": [],
                    "silero_ranges_ms": silero_ranges,
                    "segments": [],
                },
            )
        self.ensure_loaded()
        qwen_language = _qwen_language(language)
        audio_segments = [
            (
                waveform[
                    round(candidate.inference_start_ms * sample_rate / 1000) :
                    round(candidate.inference_end_ms * sample_rate / 1000)
                ],
                sample_rate,
            )
            for candidate in candidates
        ]
        results = self._model.transcribe(
            audio=audio_segments,
            language=[qwen_language] * len(audio_segments),
            return_time_stamps=True,
        )
        tokens: list[AlignedToken] = []
        raw_texts: list[str] = []
        accepted_texts: list[str] = []
        committed_token_texts: list[str] = []
        languages: list[str] = []
        raw_segments: list[dict[str, Any]] = []
        for candidate, result in zip(candidates, results, strict=True):
            text = str(result.text or "").strip()
            result_language = str(result.language or "")
            if text:
                raw_texts.append(text)
                if candidate.accepted:
                    accepted_texts.append(text)
            if result_language and result_language not in languages:
                languages.append(result_language)
            items = list(getattr(result.time_stamps, "items", []) or [])
            for item in items:
                start_seconds = (
                    candidate.inference_start_ms / 1000 + float(item.start_time)
                )
                end_seconds = (
                    candidate.inference_start_ms / 1000 + float(item.end_time)
                )
                if end_seconds <= start_seconds:
                    continue
                midpoint_ms = round((start_seconds + end_seconds) * 500)
                committed = (
                    candidate.accepted
                    and candidate.core_start_ms
                    <= midpoint_ms
                    < candidate.core_end_ms
                )
                if committed:
                    committed_token_texts.append(str(item.text))
                tokens.append(
                    AlignedToken(
                        text=str(item.text),
                        start_seconds=start_seconds,
                        end_seconds=end_seconds,
                        metadata={
                            "timestamp_source": self.alignment_model_id,
                            "speech_gate_candidate_index": candidate.candidate_index,
                            "speech_gate_accepted": candidate.accepted,
                            "speech_gate_committed": committed,
                            "gate_core_start_ms": candidate.core_start_ms,
                            "gate_core_end_ms": candidate.core_end_ms,
                            "utterance_start_ms": candidate.inference_start_ms,
                            "utterance_end_ms": candidate.inference_end_ms,
                        },
                    )
                )
            raw_segments.append(
                {
                    **asdict(candidate),
                    "language": result_language,
                    "text": text,
                    "aligned_token_count": len(items),
                }
            )
        accepted_context_text = "".join(accepted_texts)
        committed_text = "".join(committed_token_texts)
        raw = {
            "language": ",".join(languages),
            "text": committed_text,
            "raw_text": "".join(raw_texts),
            "accepted_context_text": accepted_context_text,
            "committed_token_text": committed_text,
            "speech_ranges_ms": speech_ranges,
            "candidate_ranges_ms": [
                [candidate.core_start_ms, candidate.core_end_ms]
                for candidate in candidates
            ],
            "inference_ranges_ms": [
                [candidate.inference_start_ms, candidate.inference_end_ms]
                for candidate in candidates
            ],
            "silero_ranges_ms": silero_ranges,
            "segments": raw_segments,
            "time_stamps": [asdict(token) for token in tokens],
        }
        return HypothesisResult(
            text=committed_text,
            language=",".join(languages) or None,
            tokens=tuple(tokens),
            raw_response=raw,
        )

    def _detect_fsmn_speech(
        self, audio_path: Path, duration_ms: int
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
            max_single_segment_time=self.speech_gate.max_utterance_ms,
        )
        values = response[0].get("value", []) if response else []
        raw = [
            (max(0, int(value[0])), int(value[1]))
            for value in values
            if isinstance(value, (list, tuple))
            and len(value) >= 2
            and int(value[1]) > int(value[0])
        ]
        return _merge_speech_ranges(
            raw,
            duration_ms,
            merge_gap_ms=self.speech_gate.fsmn_merge_gap_ms,
            max_utterance_ms=self.speech_gate.max_utterance_ms,
        )

    def _detect_silero_speech(
        self, waveform: np.ndarray, sample_rate: int
    ) -> list[tuple[int, int]]:
        import torch
        from silero_vad import get_speech_timestamps, load_silero_vad

        if self._silero_model is None:
            self._silero_model = load_silero_vad()
        timestamps = get_speech_timestamps(
            torch.from_numpy(np.ascontiguousarray(waveform, dtype=np.float32)),
            self._silero_model,
            sampling_rate=sample_rate,
            threshold=self.speech_gate.silero_threshold,
            min_speech_duration_ms=self.speech_gate.silero_min_speech_ms,
            min_silence_duration_ms=self.speech_gate.silero_min_silence_ms,
            speech_pad_ms=0,
            return_seconds=False,
        )
        return [
            (
                round(int(item["start"]) * 1000 / sample_rate),
                round(int(item["end"]) * 1000 / sample_rate),
            )
            for item in timestamps
            if int(item["end"]) > int(item["start"])
        ]

    def parameters(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "dtype": self.dtype_name,
            "max_inference_batch_size": self.batch_size,
            "max_new_tokens": self.max_new_tokens,
            "return_time_stamps": True,
            "segmentation": "v2c3-fsmn-proposals-silero-evidence-snr-gate-v1",
            "vad_device": "cpu",
            "speech_gate": asdict(self.speech_gate),
            "speech_gate_policy": (
                "accept FSMN proposal when duration, relative SNR, or Silero overlap "
                "passes; infer with context but commit aligned tokens only inside "
                "the unpadded accepted core"
            ),
            "silero_vad_package_version": importlib.metadata.version("silero-vad"),
            "package": "qwen-asr",
            "package_version": importlib.metadata.version("qwen-asr"),
        }

    def close(self) -> None:
        self._model = None
        self._vad_model = None
        self._silero_model = None
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
    merged = _merge_speech_ranges(
        ranges,
        duration_ms,
        merge_gap_ms=600,
        max_utterance_ms=30_000,
    )
    return _pad_non_overlapping_ranges(merged, duration_ms, padding_ms=padding_ms)


def _merge_speech_ranges(
    ranges: list[tuple[int, int]],
    duration_ms: int,
    *,
    merge_gap_ms: int,
    max_utterance_ms: int,
) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        start = max(0, min(duration_ms, start))
        end = max(0, min(duration_ms, end))
        if end <= start:
            continue
        if (
            merged
            and start - merged[-1][1] <= merge_gap_ms
            and end - merged[-1][0] <= max_utterance_ms
        ):
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _pad_non_overlapping_ranges(
    ranges: list[tuple[int, int]], duration_ms: int, *, padding_ms: int
) -> list[tuple[int, int]]:
    if not ranges:
        return []
    padded = [
        [max(0, start - padding_ms), min(duration_ms, end + padding_ms)]
        for start, end in ranges
    ]
    for index in range(len(padded) - 1):
        if padded[index][1] <= padded[index + 1][0]:
            continue
        boundary = round((ranges[index][1] + ranges[index + 1][0]) / 2)
        padded[index][1] = boundary
        padded[index + 1][0] = boundary
    return [(start, end) for start, end in padded if end > start]


def _build_speech_gate(
    waveform: np.ndarray,
    sample_rate: int,
    fsmn_ranges: list[tuple[int, int]],
    silero_ranges: list[tuple[int, int]],
    *,
    settings: SpeechGateSettings,
) -> tuple[list[SpeechGateCandidate], list[tuple[int, int]]]:
    duration_ms = round(len(waveform) * 1000 / sample_rate)
    core_ranges = _merge_speech_ranges(
        fsmn_ranges,
        duration_ms,
        merge_gap_ms=settings.fsmn_merge_gap_ms,
        max_utterance_ms=settings.max_utterance_ms,
    )
    inference_ranges = _pad_non_overlapping_ranges(
        core_ranges,
        duration_ms,
        padding_ms=settings.inference_padding_ms,
    )
    noise_floor_dbfs = _noise_floor_dbfs(waveform, sample_rate)
    candidates: list[SpeechGateCandidate] = []
    for index, ((start_ms, end_ms), (inference_start, inference_end)) in enumerate(
        zip(core_ranges, inference_ranges, strict=True)
    ):
        samples = waveform[
            round(start_ms * sample_rate / 1000) :
            round(end_ms * sample_rate / 1000)
        ]
        rms_dbfs = _rms_dbfs(samples)
        snr_db = rms_dbfs - noise_floor_dbfs
        silero_overlap_ms = _ranges_overlap_ms(
            start_ms, end_ms, silero_ranges
        )
        reasons: list[str] = []
        if end_ms - start_ms >= settings.min_candidate_ms:
            reasons.append("duration")
        if snr_db >= settings.min_snr_db:
            reasons.append("relative_snr")
        if silero_overlap_ms >= settings.min_silero_overlap_ms:
            reasons.append("silero_confirmation")
        candidates.append(
            SpeechGateCandidate(
                candidate_index=index,
                core_start_ms=start_ms,
                core_end_ms=end_ms,
                inference_start_ms=inference_start,
                inference_end_ms=inference_end,
                duration_ms=end_ms - start_ms,
                rms_dbfs=rms_dbfs,
                noise_floor_dbfs=noise_floor_dbfs,
                snr_db=snr_db,
                silero_overlap_ms=silero_overlap_ms,
                accepted=bool(reasons),
                acceptance_reasons=tuple(reasons),
            )
        )
    speech_ranges = _merge_speech_ranges(
        [
            (
                candidate.core_start_ms - settings.speech_output_padding_ms,
                candidate.core_end_ms + settings.speech_output_padding_ms,
            )
            for candidate in candidates
            if candidate.accepted
        ],
        duration_ms,
        merge_gap_ms=0,
        max_utterance_ms=duration_ms,
    )
    return candidates, speech_ranges


def _noise_floor_dbfs(waveform: np.ndarray, sample_rate: int) -> float:
    frame_samples = max(1, round(sample_rate * 0.1))
    values = [
        _rms_dbfs(waveform[start : start + frame_samples])
        for start in range(0, len(waveform) - frame_samples + 1, frame_samples)
    ]
    if not values:
        return _rms_dbfs(waveform)
    return float(np.percentile(np.asarray(values, dtype=np.float32), 20))


def _rms_dbfs(samples: np.ndarray) -> float:
    if len(samples) == 0:
        return -160.0
    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
    return 20 * math.log10(max(rms, 1e-8))


def _ranges_overlap_ms(
    start_ms: int, end_ms: int, ranges: list[tuple[int, int]]
) -> int:
    return sum(
        max(0, min(end_ms, range_end) - max(start_ms, range_start))
        for range_start, range_end in ranges
    )
