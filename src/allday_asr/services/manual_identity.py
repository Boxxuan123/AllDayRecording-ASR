"""Compatibility exports for manual speaker-identity annotations."""

from allday_asr.application.diarization.manual_identity import (
    MAX_IDENTITY_INTERVAL_MS,
    MIN_IDENTITY_INTERVAL_MS,
    MIN_SPEECH_COVERAGE,
    SECONDARY_SPEAKER_LIMIT_MS,
    manual_identity_overview,
    retract_manual_identity_annotation,
    save_manual_identity_annotation,
)

__all__ = [
    "MAX_IDENTITY_INTERVAL_MS",
    "MIN_IDENTITY_INTERVAL_MS",
    "MIN_SPEECH_COVERAGE",
    "SECONDARY_SPEAKER_LIMIT_MS",
    "manual_identity_overview",
    "retract_manual_identity_annotation",
    "save_manual_identity_annotation",
]
