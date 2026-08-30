"""Published SQLite schema and migration SQL; append new versions only."""

from .v001_initial import SQL as SCHEMA
from .v002_processing_runs import SQL as V002_SQL
from .v003_actions import SQL as V003_SQL
from .v004_source_graph import SQL as V004_SQL
from .v005_benchmark import SQL as V005_SQL
from .v006_asr_evidence import SQL as V006_SQL
from .v007_diarization_evidence import SQL as V007_SQL
from .v008_identity_evidence import SQL as V008_SQL
from .v009_semantic_evidence import SQL as V009_SQL
from .v010_evidence_guards import SQL as V010_SQL
from .v011_source_instances import SQL as V011_SQL
from .v012_session_backups import SQL as V012_SQL
from .v013_v2d1_reviews import SQL as V013_SQL
from .v014_v2d1_identity import SQL as V014_SQL
from .v015_manual_identity import SQL as V015_SQL

__all__ = [
    "MIGRATIONS",
    "SCHEMA",
    "V4_PROCESSING_RUN_COLUMNS",
    "V5_GUARD_SQL",
    "V5_PREDICTION_SET_COLUMNS",
    "V7_GUARD_SQL",
]

MIGRATIONS: dict[int, str] = {
    2: V002_SQL,
    3: V003_SQL,
    4: V004_SQL,
    5: V005_SQL,
    6: V006_SQL,
    7: V007_SQL,
    8: V008_SQL,
    9: V009_SQL,
    10: V010_SQL,
    11: V011_SQL,
    12: V012_SQL,
    13: V013_SQL,
    14: V014_SQL,
    15: V015_SQL,
}

V4_PROCESSING_RUN_COLUMNS = {
    "session_id": "INTEGER REFERENCES recording_sessions(id)",
    "input_fingerprint": "TEXT",
    "model_manifest_json": "TEXT NOT NULL DEFAULT '{}'",
    "pipeline_version": "TEXT",
    "code_version": "TEXT",
    "parent_run_id": "INTEGER",
}

V5_PREDICTION_SET_COLUMNS = {
    "status": "TEXT NOT NULL DEFAULT 'frozen' CHECK(status IN ('draft', 'frozen'))",
    "frozen_at": "TEXT",
}

V5_GUARD_SQL = """
    DROP TRIGGER IF EXISTS protect_prediction_sets_from_update;

    CREATE TRIGGER IF NOT EXISTS protect_frozen_prediction_sets_from_update
    BEFORE UPDATE ON benchmark_prediction_sets WHEN OLD.status = 'frozen'
    BEGIN
        SELECT RAISE(ABORT, 'benchmark prediction set cannot be changed');
    END;

    CREATE TRIGGER IF NOT EXISTS protect_frozen_truth_annotations_from_insert
    BEFORE INSERT ON truth_annotations
    WHEN (SELECT status FROM truth_sets WHERE id = NEW.truth_set_id) = 'frozen'
    BEGIN
        SELECT RAISE(ABORT, 'cannot append to frozen truth set');
    END;

    CREATE TRIGGER IF NOT EXISTS protect_frozen_truth_sources_from_insert
    BEFORE INSERT ON truth_annotation_sources
    WHEN (
        SELECT ts.status
        FROM truth_sets ts
        JOIN truth_annotations ta ON ta.truth_set_id = ts.id
        WHERE ta.id = NEW.annotation_id
    ) = 'frozen'
    BEGIN
        SELECT RAISE(ABORT, 'cannot append source reference to frozen truth set');
    END;

    CREATE TRIGGER IF NOT EXISTS protect_predictions_from_insert
    BEFORE INSERT ON benchmark_predictions
    WHEN (
        SELECT status FROM benchmark_prediction_sets
        WHERE id = NEW.prediction_set_id
    ) = 'frozen'
    BEGIN
        SELECT RAISE(ABORT, 'cannot append to frozen benchmark prediction set');
    END;
"""

V7_GUARD_SQL = """
    CREATE TRIGGER IF NOT EXISTS protect_diarization_turns_from_late_insert
    BEFORE INSERT ON diarization_turns
    WHEN COALESCE(
        (SELECT status FROM processing_runs WHERE id = NEW.run_id), 'missing'
    ) != 'running'
    BEGIN
        SELECT RAISE(ABORT, 'cannot append to a sealed diarization run');
    END;

    CREATE TRIGGER IF NOT EXISTS protect_diarization_turn_sources_from_late_insert
    BEFORE INSERT ON diarization_turn_sources
    WHEN COALESCE(
        (
            SELECT pr.status
            FROM processing_runs pr
            JOIN diarization_turns dt ON dt.run_id = pr.id
            WHERE dt.id = NEW.turn_id
        ),
        'missing'
    ) != 'running'
    BEGIN
        SELECT RAISE(ABORT, 'cannot append source trace to a sealed diarization run');
    END;

    CREATE TRIGGER IF NOT EXISTS protect_token_speaker_attributions_from_late_insert
    BEFORE INSERT ON token_speaker_attributions
    WHEN COALESCE(
        (SELECT status FROM processing_runs WHERE id = NEW.run_id), 'missing'
    ) != 'running'
    BEGIN
        SELECT RAISE(ABORT, 'cannot append attribution to a sealed diarization run');
    END;
"""
