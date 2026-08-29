from __future__ import annotations


def resolve_vram_profile(requested: str, *, device: str = "auto") -> str:
    """Choose batch profile from physical VRAM without changing model precision."""
    if requested in {"quality-16gb", "compatible-8gb"}:
        return requested
    if requested != "auto":
        raise ValueError("显存 profile 必须是 auto、quality-16gb 或 compatible-8gb")
    if device.strip().lower() == "cpu":
        return "compatible-8gb"
    try:
        import torch

        if not torch.cuda.is_available():
            return "compatible-8gb"
        if device.startswith("cuda:"):
            index = int(device.split(":", 1)[1])
        else:
            index = torch.cuda.current_device()
        total_bytes = int(torch.cuda.get_device_properties(index).total_memory)
    except (ImportError, RuntimeError, ValueError):
        return "compatible-8gb"
    return "quality-16gb" if total_bytes >= 14 * 1024**3 else "compatible-8gb"
