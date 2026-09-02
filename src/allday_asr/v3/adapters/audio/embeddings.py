from __future__ import annotations

import math

import numpy as np


def normalize_speech_level(
    samples: np.ndarray,
    *,
    target_dbfs: float = -24.0,
    max_gain_db: float = 24.0,
) -> np.ndarray:
    """Apply the existing RMS target with a fixed peak-safety ceiling."""
    value = np.asarray(samples, dtype=np.float32)
    rms = float(np.sqrt(np.mean(np.square(value), dtype=np.float64)))
    if rms <= 1e-8:
        return value
    current_dbfs = 20 * math.log10(rms)
    gain_db = min(max_gain_db, target_dbfs - current_dbfs)
    gain = 10 ** (gain_db / 20)
    peak = float(np.max(np.abs(value)))
    if peak > 0:
        gain = min(gain, 0.95 / peak)
    return np.asarray(value * gain, dtype=np.float32)


def l2_normalize(values: np.ndarray) -> np.ndarray:
    """Normalize each embedding row while preserving zero rows."""
    array = np.asarray(values, dtype=np.float32)
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    return array / np.maximum(norms, 1e-8)
