from __future__ import annotations
import json
from typing import Any


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _resolve_profile(requested: str, *, device: str) -> str:
    if requested in {"quality-16gb", "compatible-8gb"}:
        return requested
    if requested != "auto":
        raise ValueError(
            "V3 model profile must be auto, quality-16gb or compatible-8gb"
        )
    if device.strip().lower() == "cpu":
        return "compatible-8gb"
    try:
        import torch

        if not torch.cuda.is_available():
            return "compatible-8gb"
        index = (
            int(device.split(":", 1)[1])
            if device.startswith("cuda:")
            else torch.cuda.current_device()
        )
        total_bytes = int(torch.cuda.get_device_properties(index).total_memory)
    except (ImportError, RuntimeError, ValueError):
        return "compatible-8gb"
    return "quality-16gb" if total_bytes >= 14 * 1024**3 else "compatible-8gb"
