from __future__ import annotations

import gc
import importlib.metadata
import os
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from allday_asr.v3.adapters.models.funasr import resolve_device
from allday_asr.v3.paths import (
    DEFAULT_MODEL_PATHS,
    ModelPaths,
    configure_model_cache,
)


PYANNOTE_COMMUNITY_MODEL_ID = "pyannote/speaker-diarization-community-1"


@dataclass(frozen=True)
class SpeakerTurn:
    start_ms: int
    end_ms: int
    speaker_label: str
    confidence: float | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class QualityDiarizationResult:
    regular_turns: tuple[SpeakerTurn, ...]
    exclusive_turns: tuple[SpeakerTurn, ...]
    raw_response: dict[str, Any]


class QualityDiarizationBackend(Protocol):
    model_id: str
    model_revision: str | None
    backend_name: str

    def ensure_loaded(self) -> None:
        ...

    def diarize(
        self,
        audio_path: Path,
        *,
        num_speakers: int | None = None,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
    ) -> QualityDiarizationResult:
        ...

    def parameters(self) -> dict[str, Any]:
        ...

    def close(self) -> None:
        ...


class PyannoteCommunityBackend:
    """Local Community-1 inference with overlap and ASR-friendly exclusive tracks."""

    backend_name = "pyannote-audio-community-1"

    def __init__(
        self,
        *,
        model_id: str = PYANNOTE_COMMUNITY_MODEL_ID,
        model_path: Path | None = None,
        device: str = "auto",
        token_env: str = "HF_TOKEN",
        paths: ModelPaths | None = None,
    ) -> None:
        self.paths = paths or DEFAULT_MODEL_PATHS
        self.model_id = model_id
        self.model_path = model_path.resolve() if model_path is not None else None
        self.device = resolve_device(device)
        self.token_env = token_env
        self._login_token: str | None = None
        self._login_token_resolved = False
        self.model_revision: str | None = None
        self._pipeline = None
        self._model_source_kind = "unresolved"

    @property
    def package_version(self) -> str:
        return importlib.metadata.version("pyannote.audio")

    def ensure_loaded(self) -> None:
        if self._pipeline is not None:
            return
        # Read the standard Hugging Face login before redirecting model caches.
        # HF_HOME also controls token lookup, so the order is intentional.
        from huggingface_hub import get_token

        if not self._login_token_resolved:
            self._login_token = get_token()
            self._login_token_resolved = True
        configure_model_cache(self.paths)
        # Local private recordings must not emit usage telemetry.
        os.environ["PYANNOTE_METRICS_ENABLED"] = "false"
        source = self._resolve_model_source()
        token = (
            None
            if isinstance(source, Path)
            else os.environ.get(self.token_env) or self._login_token
        )
        if not isinstance(source, Path) and not token:
            from huggingface_hub import get_token

            token = get_token()
        if not isinstance(source, Path) and not token:
            raise RuntimeError(
                f"Community-1 尚未缓存，且没有可用的 Hugging Face 凭据（{self.token_env} "
                "或 hf auth login）。请先接受模型条款并登录，或使用 --model-path。"
            )
        import torch
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore", message="(?s).*torchcodec is not installed correctly.*"
            )
            from pyannote.audio import Pipeline
            from pyannote.audio.telemetry import set_telemetry_metrics

        set_telemetry_metrics(False)
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore", message=r"std\(\): degrees of freedom is <= 0.*"
                )
                pipeline = Pipeline.from_pretrained(
                    source,
                    token=token,
                    cache_dir=self.paths.model_dir / "huggingface" / "hub",
                )
                if pipeline is not None:
                    pipeline.to(torch.device(self.device))
        except Exception as exc:
            raise RuntimeError(
                "无法加载 pyannote Community-1，请检查本地模型和依赖。若模型尚未缓存，"
                "请先在 "
                "https://huggingface.co/pyannote/speaker-diarization-community-1 "
                f"接受模型条款，并设置 {self.token_env}；也可以配置已下载的 model_path。"
            ) from exc
        if pipeline is None:
            raise RuntimeError(
                "pyannote Community-1 返回空 pipeline；请确认已接受模型条款并有读取权限"
            )
        self._pipeline = pipeline
        if isinstance(source, Path):
            self.model_revision = source.name
        else:
            cached = self._latest_cached_snapshot()
            self.model_revision = (
                cached.name
                if cached is not None
                else getattr(pipeline, "_otel_origin", None)
            )

    def diarize(
        self,
        audio_path: Path,
        *,
        num_speakers: int | None = None,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
    ) -> QualityDiarizationResult:
        import soundfile as sf
        import torch

        self.ensure_loaded()
        waveform, sample_rate = sf.read(
            str(audio_path.resolve(strict=True)), dtype="float32", always_2d=True
        )
        mono = np.asarray(
            waveform[:, 0] if waveform.shape[1] == 1 else waveform.mean(axis=1),
            dtype=np.float32,
        )
        audio = {
            "waveform": torch.from_numpy(np.ascontiguousarray(mono)).unsqueeze(0),
            "sample_rate": int(sample_rate),
            "uri": audio_path.stem,
        }
        kwargs = {
            key: value
            for key, value in {
                "num_speakers": num_speakers,
                "min_speakers": min_speakers,
                "max_speakers": max_speakers,
            }.items()
            if value is not None
        }
        output = self._pipeline(audio, **kwargs)
        regular = _annotation_turns(output.speaker_diarization)
        exclusive = _annotation_turns(output.exclusive_speaker_diarization)
        labels = sorted({turn.speaker_label for turn in regular})
        return QualityDiarizationResult(
            regular_turns=tuple(regular),
            exclusive_turns=tuple(exclusive),
            raw_response={
                "regular_turn_count": len(regular),
                "exclusive_turn_count": len(exclusive),
                "speaker_labels": labels,
                "has_speaker_embeddings": output.speaker_embeddings is not None,
            },
        )

    def extract_speaker_embeddings(
        self, samples: list[np.ndarray], *, batch_size: int = 32
    ) -> np.ndarray:
        """Extract Community-1's own WeSpeaker embeddings from equal-length 16 kHz audio."""
        if not samples:
            return np.empty((0, 256), dtype=np.float32)
        self.ensure_loaded()
        lengths = {len(np.asarray(item)) for item in samples}
        if len(lengths) != 1:
            raise ValueError("Community-1 embedding 输入必须是等长的 16 kHz 波形")
        sample_count = next(iter(lengths))
        embedding_backend = self._pipeline._embedding
        if sample_count < int(embedding_backend.min_num_samples):
            raise ValueError("Community-1 embedding 输入过短")

        import torch

        outputs: list[np.ndarray] = []
        for offset in range(0, len(samples), batch_size):
            batch = np.stack(
                [
                    np.asarray(item, dtype=np.float32)
                    for item in samples[offset : offset + batch_size]
                ]
            )
            waveforms = torch.from_numpy(np.ascontiguousarray(batch)).unsqueeze(1)
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore", message=r"std\(\): degrees of freedom is <= 0.*"
                )
                outputs.append(
                    np.asarray(embedding_backend(waveforms), dtype=np.float32)
                )
        return np.vstack(outputs)

    def parameters(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "package_version": self.package_version,
            "model_source_kind": self._model_source_kind,
            "audio_input": "preloaded-float32-mono",
            "telemetry_enabled": False,
        }

    def close(self) -> None:
        self._pipeline = None
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

    def _resolve_model_source(self) -> str | Path:
        if self.model_path is not None:
            if not self.model_path.is_dir():
                raise FileNotFoundError(f"pyannote model_path 不存在：{self.model_path}")
            self._model_source_kind = "explicit-local-path"
            return self.model_path
        cached = self._latest_cached_snapshot()
        if cached is not None:
            self._model_source_kind = "project-cache"
            return cached
        self._model_source_kind = "huggingface-gated-id"
        return self.model_id

    def _latest_cached_snapshot(self) -> Path | None:
        root = (
            self.paths.model_dir
            / "huggingface"
            / "hub"
            / f"models--{self.model_id.replace('/', '--')}"
            / "snapshots"
        )
        if root.is_dir():
            snapshots = sorted(
                (path for path in root.iterdir() if (path / "config.yaml").is_file()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            if snapshots:
                return snapshots[0]
        return None


def _annotation_turns(annotation: Any) -> list[SpeakerTurn]:
    turns: list[SpeakerTurn] = []
    for segment, _, speaker in annotation.itertracks(yield_label=True):
        start_ms = max(0, round(float(segment.start) * 1000))
        end_ms = max(start_ms + 1, round(float(segment.end) * 1000))
        turns.append(
            SpeakerTurn(
                start_ms=start_ms,
                end_ms=end_ms,
                speaker_label=str(speaker),
                metadata={"timestamp_source": "pyannote.annotation"},
            )
        )
    return sorted(
        turns,
        key=lambda turn: (turn.start_ms, turn.end_ms, turn.speaker_label),
    )
