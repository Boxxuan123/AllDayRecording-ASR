from __future__ import annotations

import importlib.metadata
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from allday_asr.paths import AppPaths, DEFAULT_PATHS, configure_model_cache


VAD_MODEL_ID = "fsmn-vad"
ASR_MODEL_ID = "iic/SenseVoiceSmall"
SPEAKER_MODEL_ID = "cam++"
MODEL_CACHE_IDS = {
    VAD_MODEL_ID: "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
    ASR_MODEL_ID: ASR_MODEL_ID,
    SPEAKER_MODEL_ID: "iic/speech_campplus_sv_zh-cn_16k-common",
}
LANGUAGE_TAG = re.compile(r"<\|([a-zA-Z-]+)\|>")


@dataclass(frozen=True)
class Transcript:
    raw_text: str
    display_text: str
    language: str | None


@dataclass(frozen=True)
class DiarizationResult:
    turns: list[dict]
    speaker_centers: np.ndarray | None


def resolve_device(requested: str) -> str:
    if requested != "auto":
        return requested
    import torch

    return "cuda:0" if torch.cuda.is_available() else "cpu"


class FunASRBackend:
    def __init__(self, device: str = "auto", *, paths: AppPaths | None = None):
        self.paths = paths or DEFAULT_PATHS
        self.device = resolve_device(device)
        self._vad_model = None
        self._asr_model = None
        self._speaker_model = None

    @property
    def package_version(self) -> str:
        return importlib.metadata.version("funasr")

    def detect_speech(self, audio_path: Path) -> list[tuple[int, int]]:
        if self._vad_model is None:
            configure_model_cache(self.paths)
            from funasr import AutoModel

            self._vad_model = AutoModel(
                model=_cached_model_or_id(VAD_MODEL_ID, model_dir=self.paths.model_dir),
                device=self.device,
                disable_update=True,
                disable_pbar=True,
            )
        result = self._vad_model.generate(
            input=str(audio_path),
            batch_size=1,
            max_single_segment_time=30_000,
        )
        if not result:
            return []
        values = result[0].get("value", [])
        segments: list[tuple[int, int]] = []
        for value in values:
            if not isinstance(value, (list, tuple)) or len(value) < 2:
                continue
            start_ms, end_ms = int(value[0]), int(value[1])
            if end_ms > start_ms:
                segments.append((start_ms, end_ms))
        return segments

    def ensure_asr_loaded(self) -> None:
        """Download and load the ASR model before changing any segment state."""
        if self._asr_model is None:
            configure_model_cache(self.paths)
            from funasr import AutoModel

            self._asr_model = AutoModel(
                model=_cached_model_or_id(ASR_MODEL_ID, model_dir=self.paths.model_dir),
                device=self.device,
                disable_update=True,
                disable_pbar=True,
            )

    def transcribe(self, samples: np.ndarray, language: str = "zh") -> Transcript:
        self.ensure_asr_loaded()
        from funasr.utils.postprocess_utils import rich_transcription_postprocess

        result = self._asr_model.generate(
            input=samples,
            batch_size=1,
            language=language,
            use_itn=True,
        )
        item = result[0] if result else {}
        raw_text = str(item.get("text", "")).strip()
        display_text = rich_transcription_postprocess(raw_text).strip() if raw_text else ""
        language = _extract_language(raw_text)
        return Transcript(raw_text=raw_text, display_text=display_text, language=language)

    def ensure_speaker_loaded(self) -> None:
        if self._speaker_model is None:
            configure_model_cache(self.paths)
            from funasr import AutoModel

            self._speaker_model = AutoModel(
                model=_cached_model_or_id(
                    SPEAKER_MODEL_ID, model_dir=self.paths.model_dir
                ),
                device=self.device,
                disable_update=True,
                disable_pbar=True,
            )

    def extract_speaker_embeddings(
        self, samples: list[np.ndarray], *, batch_size: int = 8
    ) -> np.ndarray:
        """Extract one CAM++ embedding for each mono 16 kHz float waveform."""
        if not samples:
            return np.empty((0, 192), dtype=np.float32)
        self.ensure_speaker_loaded()
        result = self._speaker_model.generate(
            input=[np.asarray(item, dtype=np.float32) for item in samples],
            batch_size=batch_size,
        )
        embeddings: list[np.ndarray] = []
        for item in result:
            value = item.get("spk_embedding")
            if value is None:
                continue
            if hasattr(value, "detach"):
                value = value.detach().cpu().numpy()
            array = np.asarray(value, dtype=np.float32)
            if array.ndim == 1:
                array = array[None, :]
            embeddings.append(array)
        if not embeddings:
            raise RuntimeError("CAM++ 未返回声纹 embedding")
        combined = np.concatenate(embeddings, axis=0)
        if combined.shape[0] != len(samples):
            raise RuntimeError(
                f"CAM++ embedding 数量不一致：expected={len(samples)} actual={combined.shape[0]}"
            )
        return combined

    def diarize(
        self, audio_path: Path, preset_speakers: int | None = None
    ) -> DiarizationResult:
        """Run the supported FunASR VAD + SenseVoice + CAM++ meeting pipeline."""
        configure_model_cache(self.paths)
        from funasr import AutoModel

        pipeline = AutoModel(
            model=_cached_model_or_id(ASR_MODEL_ID, model_dir=self.paths.model_dir),
            vad_model=_cached_model_or_id(VAD_MODEL_ID, model_dir=self.paths.model_dir),
            spk_model=_cached_model_or_id(
                SPEAKER_MODEL_ID, model_dir=self.paths.model_dir
            ),
            device=self.device,
            spk_mode="vad_segment",
            vad_kwargs={"max_single_segment_time": 30_000},
            disable_update=True,
            disable_pbar=True,
        )
        kwargs = {
            "input": str(audio_path),
            "batch_size_s": 300,
            "language": "zh",
            "use_itn": True,
            "return_spk_res": True,
            "return_spk_center": True,
        }
        if preset_speakers is not None:
            kwargs["preset_spk_num"] = preset_speakers
        result = pipeline.generate(**kwargs)
        item = result[0] if result else {}
        turns = list(item.get("sentence_info") or [])
        centers = item.get("spk_embedding_center")
        if centers is not None:
            centers = np.asarray(centers, dtype=np.float32)
        return DiarizationResult(turns=turns, speaker_centers=centers)


def _extract_language(text: str) -> str | None:
    match = LANGUAGE_TAG.search(text)
    if not match:
        return None
    value = match.group(1).lower()
    if value in {"zh", "en", "ja", "ko", "yue"}:
        return value
    return None


def _cached_model_or_id(model_id: str, *, model_dir: Path | None = None) -> str:
    """Use an already downloaded ModelScope snapshot without a network check."""
    cache_id = MODEL_CACHE_IDS.get(model_id, model_id)
    cache_root = model_dir or DEFAULT_PATHS.model_dir
    model_root = (
        cache_root / "modelscope" / "models" / cache_id.replace("/", "--") / "snapshots"
    )
    preferred = model_root / "master"
    if _is_model_snapshot(preferred):
        return str(preferred)
    if model_root.is_dir():
        snapshots = sorted(
            (path for path in model_root.iterdir() if _is_model_snapshot(path)),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if snapshots:
            return str(snapshots[0])
    return model_id


def _is_model_snapshot(path: Path) -> bool:
    return path.is_dir() and (
        (path / "configuration.json").is_file()
        or (path / "model.pt").is_file()
        or any(path.glob("*.bin"))
    )
