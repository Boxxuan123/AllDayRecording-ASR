"""SQLite compatibility entry for the pure annotation sample planner."""

from allday_asr.v3.domain.annotation_sample_planning import (
    PREPROCESS_VERSION as PREPROCESS_VERSION,
    SamplePlan as SamplePlan,
    compute_plans,
)
from .annotation_sample_snapshot import load_snapshot


def plans(connection, evidence, session_id, model, version):
    return compute_plans(load_snapshot(connection, session_id), model, version)
