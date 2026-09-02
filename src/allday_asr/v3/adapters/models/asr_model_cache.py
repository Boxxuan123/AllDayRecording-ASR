from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from allday_asr.v3.paths import (
    DEFAULT_MODEL_PATHS,
)


def _json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return {"repr": repr(value)}


def _cached_modelscope_or_id(model_id: str, *, model_dir: Path | None = None) -> str:
    cache_root = model_dir or DEFAULT_MODEL_PATHS.model_dir
    root = (
        cache_root / "modelscope" / "models" / model_id.replace("/", "--") / "snapshots"
    )
    if root.is_dir():
        snapshots = sorted(
            (path for path in root.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if snapshots:
            return str(snapshots[0])
    return model_id


def _cached_huggingface_or_id(model_id: str, *, model_dir: Path | None = None) -> str:
    cache_root = model_dir or DEFAULT_MODEL_PATHS.model_dir
    root = (
        cache_root
        / "huggingface"
        / "hub"
        / f"models--{model_id.replace('/', '--')}"
        / "snapshots"
    )
    if root.is_dir():
        snapshots = sorted(
            (path for path in root.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if snapshots:
            return str(snapshots[0])
    modelscope_path = _cached_modelscope_or_id(model_id, model_dir=cache_root)
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
