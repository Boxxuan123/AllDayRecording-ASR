from __future__ import annotations


def metric(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.4f}"


__all__ = ["metric"]
