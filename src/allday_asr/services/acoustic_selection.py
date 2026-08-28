from __future__ import annotations

import hashlib
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class AcousticWindowScore:
    start_ms: int
    end_ms: int
    rms_dbfs: float
    p90_dbfs: float
    active_fraction: float
    longest_active_run_fraction: float
    voice_band_ratio: float
    score: float
    score_rank: int

    def as_dict(self) -> dict[str, int | float]:
        return {
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "rms_dbfs": self.rms_dbfs,
            "p90_dbfs": self.p90_dbfs,
            "active_fraction": self.active_fraction,
            "longest_active_run_fraction": self.longest_active_run_fraction,
            "voice_band_ratio": self.voice_band_ratio,
            "score": self.score,
            "score_rank": self.score_rank,
        }


@dataclass(frozen=True)
class AcousticProfile:
    sample_rate: int
    frame_ms: int
    candidate_window_ms: int
    global_noise_floor_dbfs: float
    activity_threshold_dbfs: float
    candidates: tuple[AcousticWindowScore, ...]


@dataclass(frozen=True)
class AcousticSelection:
    selected: tuple[AcousticWindowScore, ...]
    requested_gap_ms: int
    effective_gap_ms: int


@dataclass(frozen=True)
class _RawCandidate:
    start_ms: int
    end_ms: int
    rms_dbfs: float
    p90_dbfs: float
    voice_band_ratio: float
    frame_dbfs: np.ndarray


def analyze_pcm_wav(
    path: Path,
    *,
    candidate_window_ms: int,
    frame_ms: int = 100,
) -> AcousticProfile:
    """Score fixed windows from waveform features without speech or ASR models."""
    if candidate_window_ms <= 0 or frame_ms <= 0:
        raise ValueError("candidate_window_ms 和 frame_ms 必须大于 0")
    if candidate_window_ms % frame_ms:
        raise ValueError("candidate_window_ms 必须是 frame_ms 的整数倍")

    raw_candidates: list[_RawCandidate] = []
    all_frame_levels: list[np.ndarray] = []
    with wave.open(str(path.resolve(strict=True)), "rb") as audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2:
            raise ValueError("声学选择器只接受 16-bit 单声道 PCM WAV")
        sample_rate = audio.getframerate()
        frame_samples = round(sample_rate * frame_ms / 1_000)
        candidate_samples = round(sample_rate * candidate_window_ms / 1_000)
        if frame_samples <= 0 or candidate_samples <= 0:
            raise ValueError("声学选择窗口小于一个采样点")
        position = 0
        while True:
            payload = audio.readframes(candidate_samples)
            samples = np.frombuffer(payload, dtype="<i2")
            if len(samples) < candidate_samples:
                break
            normalized = samples.astype(np.float32) / 32768.0
            usable = (len(normalized) // frame_samples) * frame_samples
            frames = normalized[:usable].reshape(-1, frame_samples)
            frame_rms = np.sqrt(np.mean(np.square(frames), axis=1, dtype=np.float64))
            frame_dbfs = 20.0 * np.log10(np.maximum(frame_rms, 1e-7))
            rms = float(np.sqrt(np.mean(np.square(normalized), dtype=np.float64)))
            start_ms = position * candidate_window_ms
            raw_candidates.append(
                _RawCandidate(
                    start_ms=start_ms,
                    end_ms=start_ms + candidate_window_ms,
                    rms_dbfs=_rounded_dbfs(rms),
                    p90_dbfs=round(float(np.percentile(frame_dbfs, 90)), 3),
                    voice_band_ratio=round(
                        _voice_band_ratio(normalized, sample_rate), 6
                    ),
                    frame_dbfs=frame_dbfs,
                )
            )
            all_frame_levels.append(frame_dbfs)
            position += 1

    if not raw_candidates:
        raise ValueError("录音短于一个候选窗口，无法建立 V2-C.2 任务")
    global_noise = float(np.percentile(np.concatenate(all_frame_levels), 20))
    activity_threshold = min(-25.0, max(-65.0, global_noise + 10.0))
    metrics: list[dict[str, float]] = []
    for candidate in raw_candidates:
        active = candidate.frame_dbfs >= activity_threshold
        metrics.append(
            {
                "active_fraction": float(np.mean(active)),
                "longest_active_run_fraction": _longest_true_run(active) / len(active),
                "rms_dbfs": candidate.rms_dbfs,
                "p90_dbfs": candidate.p90_dbfs,
                "voice_band_ratio": candidate.voice_band_ratio,
            }
        )

    active_ranks = _percentile_ranks([item["active_fraction"] for item in metrics])
    run_ranks = _percentile_ranks(
        [item["longest_active_run_fraction"] for item in metrics]
    )
    p90_ranks = _percentile_ranks([item["p90_dbfs"] for item in metrics])
    rms_ranks = _percentile_ranks([item["rms_dbfs"] for item in metrics])
    voice_ranks = _percentile_ranks([item["voice_band_ratio"] for item in metrics])
    scores = [
        0.50 * active_ranks[index]
        + 0.20 * run_ranks[index]
        + 0.15 * p90_ranks[index]
        + 0.10 * rms_ranks[index]
        + 0.05 * voice_ranks[index]
        for index in range(len(raw_candidates))
    ]
    ordered = sorted(range(len(scores)), key=lambda index: (-scores[index], index))
    score_ranks = {candidate_index: rank + 1 for rank, candidate_index in enumerate(ordered)}
    candidates = tuple(
        AcousticWindowScore(
            start_ms=candidate.start_ms,
            end_ms=candidate.end_ms,
            rms_dbfs=candidate.rms_dbfs,
            p90_dbfs=candidate.p90_dbfs,
            active_fraction=round(metrics[index]["active_fraction"], 6),
            longest_active_run_fraction=round(
                metrics[index]["longest_active_run_fraction"], 6
            ),
            voice_band_ratio=candidate.voice_band_ratio,
            score=round(scores[index], 9),
            score_rank=score_ranks[index],
        )
        for index, candidate in enumerate(raw_candidates)
    )
    return AcousticProfile(
        sample_rate=sample_rate,
        frame_ms=frame_ms,
        candidate_window_ms=candidate_window_ms,
        global_noise_floor_dbfs=round(global_noise, 3),
        activity_threshold_dbfs=round(activity_threshold, 3),
        candidates=candidates,
    )


def select_acoustic_windows(
    profile: AcousticProfile,
    *,
    count: int,
    seed: str,
    minimum_gap_ms: int,
) -> AcousticSelection:
    if count <= 0:
        raise ValueError("V2-C.2 至少需要一个盲标窗口")
    if count > len(profile.candidates):
        raise ValueError("请求的盲标窗口数量超过可用声学候选")
    if minimum_gap_ms < 0:
        raise ValueError("minimum_gap_ms 不能小于 0")
    ordered = sorted(
        profile.candidates,
        key=lambda item: (
            -item.score,
            _tie_break(seed, item.start_ms, item.end_ms),
            item.start_ms,
        ),
    )
    effective_gap = minimum_gap_ms
    selected: list[AcousticWindowScore] = []
    while True:
        selected = []
        for candidate in ordered:
            if all(
                candidate.end_ms + effective_gap <= existing.start_ms
                or existing.end_ms + effective_gap <= candidate.start_ms
                for existing in selected
            ):
                selected.append(candidate)
                if len(selected) == count:
                    break
        if len(selected) == count or effective_gap == 0:
            break
        effective_gap = max(0, effective_gap - profile.candidate_window_ms)
    if len(selected) != count:
        raise RuntimeError("无法选择足够的非重叠声学窗口")
    return AcousticSelection(
        selected=tuple(sorted(selected, key=lambda item: item.start_ms)),
        requested_gap_ms=minimum_gap_ms,
        effective_gap_ms=effective_gap,
    )


def _voice_band_ratio(samples: np.ndarray, sample_rate: int) -> float:
    spectral_samples = max(1, round(sample_rate * 0.5))
    usable = (len(samples) // spectral_samples) * spectral_samples
    if usable == 0:
        return 0.0
    frames = samples[:usable].reshape(-1, spectral_samples)
    window = np.hanning(spectral_samples).astype(np.float32)
    spectrum = np.abs(np.fft.rfft(frames * window, axis=1)) ** 2
    frequencies = np.fft.rfftfreq(spectral_samples, d=1.0 / sample_rate)
    audible = (frequencies >= 80) & (frequencies <= min(7_500, sample_rate / 2))
    voice = (frequencies >= 200) & (frequencies <= min(4_000, sample_rate / 2))
    total_energy = np.sum(spectrum[:, audible], axis=1)
    voice_energy = np.sum(spectrum[:, voice], axis=1)
    ratios = np.divide(
        voice_energy,
        total_energy,
        out=np.zeros_like(voice_energy),
        where=total_energy > 0,
    )
    return float(np.median(ratios))


def _rounded_dbfs(linear_rms: float) -> float:
    return round(float(20.0 * np.log10(max(linear_rms, 1e-7))), 3)


def _longest_true_run(values: np.ndarray) -> int:
    longest = current = 0
    for value in values:
        if bool(value):
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _percentile_ranks(values: list[float]) -> list[float]:
    if len(values) == 1:
        return [1.0]
    ordered = sorted(range(len(values)), key=lambda index: (values[index], index))
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(ordered):
        end = cursor + 1
        value = values[ordered[cursor]]
        while end < len(ordered) and values[ordered[end]] == value:
            end += 1
        average_rank = ((cursor + end - 1) / 2) / (len(values) - 1)
        for position in range(cursor, end):
            ranks[ordered[position]] = average_rank
        cursor = end
    return ranks


def _tie_break(seed: str, start_ms: int, end_ms: int) -> str:
    return hashlib.sha256(f"{seed}:{start_ms}:{end_ms}".encode()).hexdigest()
