from __future__ import annotations
import importlib.metadata
from dataclasses import asdict
from pathlib import Path
from typing import Any
import numpy as np
from allday_asr.v3.adapters.models.funasr import _cached_model_or_id, resolve_device
from allday_asr.v3.paths import (
    DEFAULT_MODEL_PATHS,
    ModelPaths,
    configure_model_cache,
)

from .asr_types import (
    AlignedToken,
    HypothesisResult,
    QWEN_ALIGNER_MODEL_ID,
    QWEN_ASR_MODEL_ID,
    SpeechGateSettings,
)
from .asr_model_cache import _cached_huggingface_or_id, _release_cuda
from .speech_gate import _build_speech_gate, _merge_speech_ranges


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
        paths: ModelPaths | None = None,
    ):
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self.paths = paths or DEFAULT_MODEL_PATHS
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
        configure_model_cache(self.paths)
        import torch
        from qwen_asr import Qwen3ASRModel

        dtype = getattr(torch, self.dtype_name)
        model_path = _cached_huggingface_or_id(
            self.model_id, model_dir=self.paths.model_dir
        )
        aligner_path = _cached_huggingface_or_id(
            self.alignment_model_id, model_dir=self.paths.model_dir
        )
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
                    round(candidate.inference_start_ms * sample_rate / 1000) : round(
                        candidate.inference_end_ms * sample_rate / 1000
                    )
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
                start_seconds = candidate.inference_start_ms / 1000 + float(
                    item.start_time
                )
                end_seconds = candidate.inference_start_ms / 1000 + float(item.end_time)
                if end_seconds <= start_seconds:
                    continue
                midpoint_ms = round((start_seconds + end_seconds) * 500)
                committed = (
                    candidate.accepted
                    and candidate.core_start_ms <= midpoint_ms < candidate.core_end_ms
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
        if self._vad_model is None:
            configure_model_cache(self.paths)
            from funasr import AutoModel

            self._vad_model = AutoModel(
                model=_cached_model_or_id("fsmn-vad", model_dir=self.paths.model_dir),
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
        if self._silero_model is None:
            configure_model_cache(self.paths)
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
