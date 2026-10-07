from __future__ import annotations
import json
import os
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
    return _select_snapshot(model_id, root)


def _cached_huggingface_or_id(model_id: str, *, model_dir: Path | None = None) -> str:
    cache_root = model_dir or DEFAULT_MODEL_PATHS.model_dir
    root = (
        cache_root
        / "huggingface"
        / "hub"
        / f"models--{model_id.replace('/', '--')}"
        / "snapshots"
    )
    if root.is_dir() and any(path.is_dir() for path in root.iterdir()):
        return _select_snapshot(model_id, root)
    modelscope_root = (
        cache_root / "modelscope" / "models" /
        model_id.replace("/", "--") / "snapshots"
    )
    return _select_snapshot(model_id, modelscope_root)


def _select_snapshot(model_id: str, root: Path) -> str:
    """Resolve an exact snapshot, never the newest cache mtime."""
    direct = Path(model_id)
    if direct.is_dir():
        return str(direct.resolve())
    pins = json.loads(os.environ.get("ALLDAY_MODEL_SNAPSHOT_IDS", "{}"))
    if not isinstance(pins, dict):
        raise ValueError("ALLDAY_MODEL_SNAPSHOT_IDS must be a JSON object")
    pin = pins.get(model_id)
    if pin is not None:
        if not isinstance(pin, str) or not pin or "/" in pin or "\\" in pin or pin == "..":
            raise ValueError(f"invalid model snapshot pin: {model_id}")
        selected = root / pin
        if not selected.is_dir():
            raise FileNotFoundError(f"pinned model snapshot is missing: {model_id}@{pin}")
        return str(selected)
    snapshots = sorted(path for path in root.iterdir() if path.is_dir()) if root.is_dir() else []
    if len(snapshots) == 1:
        return str(snapshots[0])
    if len(snapshots) > 1:
        raise ValueError(f"multiple cached snapshots for {model_id}; set ALLDAY_MODEL_SNAPSHOT_IDS")
    if os.environ.get("ALLDAY_ALLOW_REMOTE_MODEL_FALLBACK") == "1":
        return model_id
    raise FileNotFoundError(f"no cached snapshot for {model_id}; pin or explicitly enable remote fallback")


def _release_cuda() -> None:
    import gc

    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
