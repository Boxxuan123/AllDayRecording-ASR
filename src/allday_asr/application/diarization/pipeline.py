from __future__ import annotations

from typing import Any

from allday_asr.application.diarization.contracts import (
    DEFAULT_DIARIZATION_VERSION,
    DiarizationVersion,
    require_diarization_version,
)
from allday_asr.application.diarization.versions import runner_for
from allday_asr.application.diarization.base import (
    QualityDiarizationSettings,
    QualityDiarizationSnapshotSummary,
    QualityDiarizationSummary,
    attribute_tokens_to_speakers,
    compute_overlap_regions,
    snapshot_quality_diarization,
)
from allday_asr.storage.database import Database

DiarizationSettings = QualityDiarizationSettings
DiarizationSummary = QualityDiarizationSummary
DiarizationSnapshotSummary = QualityDiarizationSnapshotSummary


def run(
    database: Database,
    recording_id: int | None,
    *,
    version: DiarizationVersion | str = DEFAULT_DIARIZATION_VERSION,
    **kwargs: Any,
) -> Any:
    resolved = require_diarization_version(version)
    return runner_for(resolved)(database, recording_id, **kwargs)


__all__ = [
    "DiarizationSettings",
    "DiarizationSnapshotSummary",
    "DiarizationSummary",
    "attribute_tokens_to_speakers",
    "compute_overlap_regions",
    "run",
    "snapshot_quality_diarization",
]
