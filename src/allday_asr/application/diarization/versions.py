from __future__ import annotations

from collections.abc import Callable
from typing import Any

from allday_asr.application.diarization.contracts import (
    DiarizationVersion,
    require_diarization_version,
)

DiarizationCallable = Callable[..., Any]


def runner_for(version: DiarizationVersion | str) -> DiarizationCallable:
    resolved = require_diarization_version(version)
    if resolved is DiarizationVersion.V2_D:
        from allday_asr.application.diarization.base import (
            run_quality_diarization,
        )

        return run_quality_diarization
    if resolved is DiarizationVersion.V2_D_1:
        from allday_asr.application.diarization.recall_pipeline import (
            run_quality_diarization_v2d1,
        )

        return run_quality_diarization_v2d1
    if resolved is DiarizationVersion.V2_D_2:
        from allday_asr.application.diarization.identity_audit import (
            run_identity_contamination_audit,
        )

        return run_identity_contamination_audit
    if resolved is DiarizationVersion.V2_D_3:
        from allday_asr.application.diarization.identity_candidates import (
            run_identity_candidate_mining,
        )

        return run_identity_candidate_mining
    raise AssertionError(f"未处理 diarization 版本：{resolved}")
