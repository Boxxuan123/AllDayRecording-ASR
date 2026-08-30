"""Compatibility exports for the frozen V2-D.2 identity audit."""

from allday_asr.application.diarization.identity_audit import (
    V2D2_CONTAMINATION_MIN_MS,
    V2D2_CONTAMINATION_MIN_SHARE,
    V2D2Summary,
    build_identity_contamination_matrix,
    run_identity_contamination_audit,
)

__all__ = [
    "V2D2_CONTAMINATION_MIN_MS",
    "V2D2_CONTAMINATION_MIN_SHARE",
    "V2D2Summary",
    "build_identity_contamination_matrix",
    "run_identity_contamination_audit",
]
