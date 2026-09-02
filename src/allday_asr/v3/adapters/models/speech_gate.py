from __future__ import annotations
import math
import numpy as np

from .asr_types import (
    SpeechGateCandidate,
    SpeechGateSettings,
)


def _padded_non_overlapping_ranges(
    ranges: list[tuple[int, int]], duration_ms: int, *, padding_ms: int
) -> list[tuple[int, int]]:
    merged = _merge_speech_ranges(
        ranges,
        duration_ms,
        merge_gap_ms=600,
        max_utterance_ms=30_000,
    )
    return _pad_non_overlapping_ranges(merged, duration_ms, padding_ms=padding_ms)


def _merge_speech_ranges(
    ranges: list[tuple[int, int]],
    duration_ms: int,
    *,
    merge_gap_ms: int,
    max_utterance_ms: int,
) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        start = max(0, min(duration_ms, start))
        end = max(0, min(duration_ms, end))
        if end <= start:
            continue
        if (
            merged
            and start - merged[-1][1] <= merge_gap_ms
            and end - merged[-1][0] <= max_utterance_ms
        ):
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _pad_non_overlapping_ranges(
    ranges: list[tuple[int, int]], duration_ms: int, *, padding_ms: int
) -> list[tuple[int, int]]:
    if not ranges:
        return []
    padded = [
        [max(0, start - padding_ms), min(duration_ms, end + padding_ms)]
        for start, end in ranges
    ]
    for index in range(len(padded) - 1):
        if padded[index][1] <= padded[index + 1][0]:
            continue
        boundary = round((ranges[index][1] + ranges[index + 1][0]) / 2)
        padded[index][1] = boundary
        padded[index + 1][0] = boundary
    return [(start, end) for start, end in padded if end > start]


def _build_speech_gate(
    waveform: np.ndarray,
    sample_rate: int,
    fsmn_ranges: list[tuple[int, int]],
    silero_ranges: list[tuple[int, int]],
    *,
    settings: SpeechGateSettings,
) -> tuple[list[SpeechGateCandidate], list[tuple[int, int]]]:
    duration_ms = round(len(waveform) * 1000 / sample_rate)
    core_ranges = _merge_speech_ranges(
        fsmn_ranges,
        duration_ms,
        merge_gap_ms=settings.fsmn_merge_gap_ms,
        max_utterance_ms=settings.max_utterance_ms,
    )
    inference_ranges = _pad_non_overlapping_ranges(
        core_ranges,
        duration_ms,
        padding_ms=settings.inference_padding_ms,
    )
    noise_floor_dbfs = _noise_floor_dbfs(waveform, sample_rate)
    candidates: list[SpeechGateCandidate] = []
    for index, ((start_ms, end_ms), (inference_start, inference_end)) in enumerate(
        zip(core_ranges, inference_ranges, strict=True)
    ):
        samples = waveform[
            round(start_ms * sample_rate / 1000) : round(end_ms * sample_rate / 1000)
        ]
        rms_dbfs = _rms_dbfs(samples)
        snr_db = rms_dbfs - noise_floor_dbfs
        silero_overlap_ms = _ranges_overlap_ms(start_ms, end_ms, silero_ranges)
        reasons: list[str] = []
        if end_ms - start_ms >= settings.min_candidate_ms:
            reasons.append("duration")
        if snr_db >= settings.min_snr_db:
            reasons.append("relative_snr")
        if silero_overlap_ms >= settings.min_silero_overlap_ms:
            reasons.append("silero_confirmation")
        candidates.append(
            SpeechGateCandidate(
                candidate_index=index,
                core_start_ms=start_ms,
                core_end_ms=end_ms,
                inference_start_ms=inference_start,
                inference_end_ms=inference_end,
                duration_ms=end_ms - start_ms,
                rms_dbfs=rms_dbfs,
                noise_floor_dbfs=noise_floor_dbfs,
                snr_db=snr_db,
                silero_overlap_ms=silero_overlap_ms,
                accepted=bool(reasons),
                acceptance_reasons=tuple(reasons),
            )
        )
    speech_ranges = _merge_speech_ranges(
        [
            (
                candidate.core_start_ms - settings.speech_output_padding_ms,
                candidate.core_end_ms + settings.speech_output_padding_ms,
            )
            for candidate in candidates
            if candidate.accepted
        ],
        duration_ms,
        merge_gap_ms=0,
        max_utterance_ms=duration_ms,
    )
    return candidates, speech_ranges


def _noise_floor_dbfs(waveform: np.ndarray, sample_rate: int) -> float:
    frame_samples = max(1, round(sample_rate * 0.1))
    values = [
        _rms_dbfs(waveform[start : start + frame_samples])
        for start in range(0, len(waveform) - frame_samples + 1, frame_samples)
    ]
    if not values:
        return _rms_dbfs(waveform)
    return float(np.percentile(np.asarray(values, dtype=np.float32), 20))


def _rms_dbfs(samples: np.ndarray) -> float:
    if len(samples) == 0:
        return -160.0
    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
    return 20 * math.log10(max(rms, 1e-8))


def _ranges_overlap_ms(
    start_ms: int, end_ms: int, ranges: list[tuple[int, int]]
) -> int:
    return sum(
        max(0, min(end_ms, range_end) - max(start_ms, range_start))
        for range_start, range_end in ranges
    )
