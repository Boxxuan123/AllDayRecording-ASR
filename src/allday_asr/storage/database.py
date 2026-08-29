from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS recordings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    device TEXT,
    recorded_at TEXT NOT NULL,
    timezone TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    codec TEXT,
    sample_rate INTEGER,
    channels INTEGER,
    bit_rate INTEGER,
    encoder TEXT,
    normalized_path TEXT,
    status TEXT NOT NULL DEFAULT 'imported',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processing_stages (
    recording_id INTEGER NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    model_id TEXT,
    model_version TEXT,
    started_at TEXT,
    completed_at TEXT,
    error TEXT,
    details_json TEXT,
    PRIMARY KEY (recording_id, stage),
    FOREIGN KEY (recording_id) REFERENCES recordings(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS person_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT NOT NULL,
    profile_type TEXT NOT NULL CHECK(profile_type IN ('self', 'known_person')),
    embedding_model TEXT,
    embedding_version TEXT,
    embedding_path TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS speech_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id INTEGER NOT NULL,
    segment_index INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    speaker_session_id TEXT,
    person_id INTEGER,
    speaker_match_score REAL,
    language TEXT,
    text_raw TEXT,
    text_display TEXT,
    asr_model TEXT,
    audio_ref TEXT NOT NULL,
    asr_status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(recording_id, segment_index),
    FOREIGN KEY (recording_id) REFERENCES recordings(id) ON DELETE CASCADE,
    FOREIGN KEY (person_id) REFERENCES person_profiles(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_segments_recording_time
ON speech_segments(recording_id, start_ms);

CREATE INDEX IF NOT EXISTS idx_segments_asr_status
ON speech_segments(recording_id, asr_status);

CREATE TABLE IF NOT EXISTS segment_identity_annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id INTEGER NOT NULL,
    segment_id INTEGER NOT NULL,
    identity_label TEXT NOT NULL,
    raw_label TEXT NOT NULL,
    confidence TEXT NOT NULL,
    annotation_source TEXT NOT NULL DEFAULT 'human',
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(recording_id, segment_id),
    FOREIGN KEY (recording_id) REFERENCES recordings(id) ON DELETE CASCADE,
    FOREIGN KEY (segment_id) REFERENCES speech_segments(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS voice_library_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_key TEXT NOT NULL UNIQUE,
    person_id INTEGER,
    identity_label TEXT NOT NULL,
    sample_type TEXT NOT NULL,
    split TEXT NOT NULL CHECK(split IN ('accepted', 'holdout', 'negative')),
    source_path TEXT NOT NULL,
    stored_path TEXT,
    source_sha256 TEXT,
    recording_id INTEGER,
    segment_id INTEGER,
    session_key TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    speech_ms INTEGER NOT NULL,
    embedding_blob BLOB,
    embedding_dim INTEGER,
    embedding_model TEXT,
    embedding_version TEXT,
    match_score REAL,
    human_confirmed INTEGER NOT NULL DEFAULT 1,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (person_id) REFERENCES person_profiles(id) ON DELETE SET NULL,
    FOREIGN KEY (recording_id) REFERENCES recordings(id) ON DELETE SET NULL,
    FOREIGN KEY (segment_id) REFERENCES speech_segments(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_voice_library_person_split
ON voice_library_samples(person_id, split);

CREATE INDEX IF NOT EXISTS idx_voice_library_identity_label
ON voice_library_samples(identity_label, split);

CREATE TABLE IF NOT EXISTS conversation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    title TEXT,
    summary TEXT,
    segment_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (recording_id) REFERENCES recordings(id) ON DELETE CASCADE
);
"""

LATEST_SCHEMA_VERSION = 11

MIGRATIONS: dict[int, str] = {
    2: """
        CREATE TABLE IF NOT EXISTS processing_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recording_id INTEGER NOT NULL,
            run_kind TEXT NOT NULL,
            status TEXT NOT NULL,
            config_json TEXT NOT NULL,
            config_sha256 TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            error TEXT,
            summary_json TEXT,
            artifacts_json TEXT,
            FOREIGN KEY (recording_id) REFERENCES recordings(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_processing_runs_recording
        ON processing_runs(recording_id, id);

        CREATE TABLE IF NOT EXISTS evaluation_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recording_id INTEGER NOT NULL,
            truth_path TEXT NOT NULL,
            truth_sha256 TEXT NOT NULL,
            config_json TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            report_json_path TEXT NOT NULL,
            report_markdown_path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (recording_id) REFERENCES recordings(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_evaluation_runs_recording
        ON evaluation_runs(recording_id, id);
    """,
    3: """
        CREATE TABLE IF NOT EXISTS action_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recording_id INTEGER NOT NULL,
            candidate_key TEXT NOT NULL UNIQUE,
            candidate_type TEXT NOT NULL CHECK(candidate_type IN ('schedule', 'todo')),
            status TEXT NOT NULL CHECK(status IN ('pending', 'confirmed', 'dismissed')),
            start_ms INTEGER NOT NULL,
            end_ms INTEGER NOT NULL,
            source_segment_ids_json TEXT NOT NULL,
            title TEXT NOT NULL,
            scheduled_at TEXT,
            time_text TEXT,
            location TEXT,
            participants_json TEXT NOT NULL,
            confidence REAL NOT NULL,
            evidence_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (recording_id) REFERENCES recordings(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_action_candidates_recording_status
        ON action_candidates(recording_id, status, start_ms);
    """,
    4: """
        CREATE TABLE IF NOT EXISTS source_objects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sha256 TEXT NOT NULL UNIQUE,
            source_path TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            byte_size INTEGER,
            container TEXT,
            codec TEXT,
            sample_rate INTEGER,
            channels INTEGER,
            bit_rate INTEGER,
            encoder TEXT,
            device TEXT,
            recorded_at TEXT NOT NULL,
            timezone TEXT NOT NULL,
            duration_ms INTEGER NOT NULL,
            ingest_method TEXT NOT NULL CHECK(
                ingest_method IN ('manual_file', 'watch_auto', 'watch_manual_sync')
            ),
            storage_class TEXT NOT NULL DEFAULT 'original_permanent' CHECK(
                storage_class = 'original_permanent'
            ),
            integrity_status TEXT NOT NULL DEFAULT 'unverified' CHECK(
                integrity_status IN ('unverified', 'verified', 'missing', 'mismatch', 'error')
            ),
            last_verified_at TEXT,
            backup_status TEXT NOT NULL DEFAULT 'not_configured' CHECK(
                backup_status IN ('not_configured', 'pending', 'verified', 'failed')
            ),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_source_objects_integrity
        ON source_objects(integrity_status, id);

        CREATE TRIGGER IF NOT EXISTS protect_source_objects_immutable_fields
        BEFORE UPDATE OF
            sha256, source_path, original_filename, byte_size, container, codec,
            sample_rate, channels, bit_rate, encoder, device, recorded_at,
            timezone, duration_ms, ingest_method, storage_class, created_at
        ON source_objects
        BEGIN
            SELECT RAISE(ABORT, 'immutable source metadata cannot be changed');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_source_objects_from_delete
        BEFORE DELETE ON source_objects
        BEGIN
            SELECT RAISE(ABORT, 'immutable source object cannot be deleted');
        END;

        CREATE TABLE IF NOT EXISTS recording_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_key TEXT NOT NULL UNIQUE,
            legacy_recording_id INTEGER UNIQUE,
            device TEXT,
            recorded_at TEXT NOT NULL,
            timezone TEXT NOT NULL,
            duration_ms INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'closed')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (legacy_recording_id) REFERENCES recordings(id) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS session_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            source_object_id INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            session_start_ms INTEGER NOT NULL,
            session_end_ms INTEGER NOT NULL,
            source_start_ms INTEGER NOT NULL DEFAULT 0,
            source_end_ms INTEGER NOT NULL,
            continuity_status TEXT NOT NULL DEFAULT 'unchecked' CHECK(
                continuity_status IN ('single', 'unchecked', 'continuous', 'gap', 'overlap')
            ),
            created_at TEXT NOT NULL,
            UNIQUE(session_id, chunk_index),
            UNIQUE(session_id, source_object_id),
            CHECK(session_end_ms > session_start_ms),
            CHECK(source_end_ms > source_start_ms),
            FOREIGN KEY (session_id) REFERENCES recording_sessions(id) ON DELETE RESTRICT,
            FOREIGN KEY (source_object_id) REFERENCES source_objects(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_session_sources_time
        ON session_sources(session_id, session_start_ms, session_end_ms);

        CREATE TABLE IF NOT EXISTS source_integrity_audits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_object_id INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('verified', 'missing', 'mismatch', 'error')),
            expected_sha256 TEXT NOT NULL,
            actual_sha256 TEXT,
            expected_byte_size INTEGER,
            actual_byte_size INTEGER,
            details_json TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            FOREIGN KEY (source_object_id) REFERENCES source_objects(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_source_integrity_audits_source
        ON source_integrity_audits(source_object_id, id);

        CREATE TABLE IF NOT EXISTS processing_run_inputs (
            run_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            source_object_id INTEGER NOT NULL,
            source_sha256 TEXT NOT NULL,
            session_start_ms INTEGER NOT NULL,
            session_end_ms INTEGER NOT NULL,
            source_start_ms INTEGER NOT NULL,
            source_end_ms INTEGER NOT NULL,
            PRIMARY KEY (run_id, position),
            FOREIGN KEY (run_id) REFERENCES processing_runs(id) ON DELETE CASCADE,
            FOREIGN KEY (source_object_id) REFERENCES source_objects(id) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS derived_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER,
            source_object_id INTEGER,
            artifact_type TEXT NOT NULL,
            artifact_path TEXT NOT NULL,
            sha256 TEXT,
            byte_size INTEGER,
            cache_key TEXT UNIQUE,
            regenerable INTEGER NOT NULL DEFAULT 1,
            session_start_ms INTEGER,
            session_end_ms INTEGER,
            metadata_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES processing_runs(id) ON DELETE SET NULL,
            FOREIGN KEY (source_object_id) REFERENCES source_objects(id) ON DELETE SET NULL
        );
    """,
    5: """
        CREATE TABLE IF NOT EXISTS truth_sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            truth_key TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            session_id INTEGER NOT NULL,
            parent_truth_set_id INTEGER,
            format_version TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('draft', 'frozen')),
            scope_start_ms INTEGER NOT NULL,
            scope_end_ms INTEGER NOT NULL,
            input_fingerprint TEXT NOT NULL,
            completeness_json TEXT NOT NULL,
            truth_path TEXT NOT NULL,
            truth_sha256 TEXT NOT NULL,
            provenance_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            frozen_at TEXT,
            CHECK(scope_end_ms > scope_start_ms),
            FOREIGN KEY (session_id) REFERENCES recording_sessions(id) ON DELETE RESTRICT,
            FOREIGN KEY (parent_truth_set_id) REFERENCES truth_sets(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_truth_sets_session
        ON truth_sets(session_id, id);

        CREATE TABLE IF NOT EXISTS truth_annotations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            truth_set_id INTEGER NOT NULL,
            annotation_key TEXT NOT NULL,
            annotation_kind TEXT NOT NULL CHECK(annotation_kind IN (
                'speech', 'non_speech', 'transcript', 'speaker', 'identity',
                'overlap', 'alignment_token', 'entity', 'uncertain'
            )),
            session_start_ms INTEGER NOT NULL,
            session_end_ms INTEGER NOT NULL,
            label TEXT,
            text TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            legacy_segment_id INTEGER,
            created_at TEXT NOT NULL,
            UNIQUE(truth_set_id, annotation_key),
            CHECK(session_end_ms > session_start_ms),
            FOREIGN KEY (truth_set_id) REFERENCES truth_sets(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_truth_annotations_time
        ON truth_annotations(truth_set_id, annotation_kind, session_start_ms, session_end_ms);

        CREATE TABLE IF NOT EXISTS truth_annotation_sources (
            annotation_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            source_object_id INTEGER NOT NULL,
            source_sha256 TEXT NOT NULL,
            source_start_ms INTEGER NOT NULL,
            source_end_ms INTEGER NOT NULL,
            PRIMARY KEY (annotation_id, position),
            CHECK(source_end_ms > source_start_ms),
            FOREIGN KEY (annotation_id) REFERENCES truth_annotations(id) ON DELETE RESTRICT,
            FOREIGN KEY (source_object_id) REFERENCES source_objects(id) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS benchmark_prediction_sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prediction_key TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            session_id INTEGER NOT NULL,
            processing_run_id INTEGER,
            input_fingerprint TEXT NOT NULL,
            adapter TEXT NOT NULL,
            model_manifest_json TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('draft', 'frozen')),
            created_at TEXT NOT NULL,
            frozen_at TEXT,
            FOREIGN KEY (session_id) REFERENCES recording_sessions(id) ON DELETE RESTRICT,
            FOREIGN KEY (processing_run_id) REFERENCES processing_runs(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_benchmark_prediction_sets_session
        ON benchmark_prediction_sets(session_id, id);

        CREATE TABLE IF NOT EXISTS benchmark_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prediction_set_id INTEGER NOT NULL,
            prediction_key TEXT NOT NULL,
            prediction_kind TEXT NOT NULL CHECK(prediction_kind IN (
                'speech', 'transcript', 'speaker', 'identity',
                'overlap', 'alignment_token', 'entity'
            )),
            session_start_ms INTEGER NOT NULL,
            session_end_ms INTEGER NOT NULL,
            label TEXT,
            text TEXT,
            confidence REAL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE(prediction_set_id, prediction_key),
            CHECK(session_end_ms > session_start_ms),
            FOREIGN KEY (prediction_set_id)
                REFERENCES benchmark_prediction_sets(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_benchmark_predictions_time
        ON benchmark_predictions(
            prediction_set_id, prediction_kind, session_start_ms, session_end_ms
        );

        CREATE TABLE IF NOT EXISTS benchmark_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            truth_set_id INTEGER NOT NULL,
            prediction_set_id INTEGER NOT NULL,
            config_json TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            details_json TEXT NOT NULL,
            report_json_path TEXT NOT NULL,
            report_markdown_path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (truth_set_id) REFERENCES truth_sets(id) ON DELETE RESTRICT,
            FOREIGN KEY (prediction_set_id)
                REFERENCES benchmark_prediction_sets(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_benchmark_runs_truth
        ON benchmark_runs(truth_set_id, id);

        CREATE TRIGGER IF NOT EXISTS protect_frozen_truth_sets_from_update
        BEFORE UPDATE ON truth_sets WHEN OLD.status = 'frozen'
        BEGIN
            SELECT RAISE(ABORT, 'frozen truth set cannot be changed');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_truth_sets_from_delete
        BEFORE DELETE ON truth_sets
        BEGIN
            SELECT RAISE(ABORT, 'truth set cannot be deleted');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_frozen_truth_annotations_from_update
        BEFORE UPDATE ON truth_annotations
        WHEN (SELECT status FROM truth_sets WHERE id = OLD.truth_set_id) = 'frozen'
        BEGIN
            SELECT RAISE(ABORT, 'frozen truth annotation cannot be changed');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_frozen_truth_annotations_from_insert
        BEFORE INSERT ON truth_annotations
        WHEN (SELECT status FROM truth_sets WHERE id = NEW.truth_set_id) = 'frozen'
        BEGIN
            SELECT RAISE(ABORT, 'cannot append to frozen truth set');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_frozen_truth_annotations_from_delete
        BEFORE DELETE ON truth_annotations
        WHEN (SELECT status FROM truth_sets WHERE id = OLD.truth_set_id) = 'frozen'
        BEGIN
            SELECT RAISE(ABORT, 'frozen truth annotation cannot be deleted');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_frozen_truth_sources_from_update
        BEFORE UPDATE ON truth_annotation_sources
        WHEN (
            SELECT ts.status
            FROM truth_sets ts
            JOIN truth_annotations ta ON ta.truth_set_id = ts.id
            WHERE ta.id = OLD.annotation_id
        ) = 'frozen'
        BEGIN
            SELECT RAISE(ABORT, 'frozen truth source reference cannot be changed');
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

        CREATE TRIGGER IF NOT EXISTS protect_frozen_truth_sources_from_delete
        BEFORE DELETE ON truth_annotation_sources
        WHEN (
            SELECT ts.status
            FROM truth_sets ts
            JOIN truth_annotations ta ON ta.truth_set_id = ts.id
            WHERE ta.id = OLD.annotation_id
        ) = 'frozen'
        BEGIN
            SELECT RAISE(ABORT, 'frozen truth source reference cannot be deleted');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_frozen_prediction_sets_from_update
        BEFORE UPDATE ON benchmark_prediction_sets WHEN OLD.status = 'frozen'
        BEGIN
            SELECT RAISE(ABORT, 'benchmark prediction set cannot be changed');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_prediction_sets_from_delete
        BEFORE DELETE ON benchmark_prediction_sets
        BEGIN
            SELECT RAISE(ABORT, 'benchmark prediction set cannot be deleted');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_predictions_from_update
        BEFORE UPDATE ON benchmark_predictions
        BEGIN
            SELECT RAISE(ABORT, 'benchmark prediction cannot be changed');
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

        CREATE TRIGGER IF NOT EXISTS protect_predictions_from_delete
        BEFORE DELETE ON benchmark_predictions
        BEGIN
            SELECT RAISE(ABORT, 'benchmark prediction cannot be deleted');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_benchmark_runs_from_update
        BEFORE UPDATE ON benchmark_runs
        BEGIN
            SELECT RAISE(ABORT, 'benchmark run cannot be changed');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_benchmark_runs_from_delete
        BEFORE DELETE ON benchmark_runs
        BEGIN
            SELECT RAISE(ABORT, 'benchmark run cannot be deleted');
        END;
    """,
    6: """
        CREATE TABLE IF NOT EXISTS asr_hypotheses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hypothesis_key TEXT NOT NULL UNIQUE,
            run_id INTEGER NOT NULL,
            session_id INTEGER NOT NULL,
            window_index INTEGER NOT NULL,
            hypothesis_role TEXT NOT NULL CHECK(
                hypothesis_role IN ('primary', 'secondary')
            ),
            core_start_ms INTEGER NOT NULL,
            core_end_ms INTEGER NOT NULL,
            analysis_start_ms INTEGER NOT NULL,
            analysis_end_ms INTEGER NOT NULL,
            model_id TEXT NOT NULL,
            model_revision TEXT,
            backend TEXT NOT NULL,
            language TEXT,
            text TEXT NOT NULL,
            parameters_json TEXT NOT NULL,
            raw_response_json TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, window_index, hypothesis_role),
            CHECK(core_end_ms > core_start_ms),
            CHECK(analysis_end_ms > analysis_start_ms),
            CHECK(analysis_start_ms <= core_start_ms),
            CHECK(analysis_end_ms >= core_end_ms),
            FOREIGN KEY (run_id) REFERENCES processing_runs(id) ON DELETE RESTRICT,
            FOREIGN KEY (session_id) REFERENCES recording_sessions(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_asr_hypotheses_run_window
        ON asr_hypotheses(run_id, window_index, hypothesis_role);

        CREATE TABLE IF NOT EXISTS asr_alignment_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hypothesis_id INTEGER NOT NULL,
            token_index INTEGER NOT NULL,
            text TEXT NOT NULL,
            session_start_ms INTEGER NOT NULL,
            session_end_ms INTEGER NOT NULL,
            analysis_start_ms INTEGER NOT NULL,
            analysis_end_ms INTEGER NOT NULL,
            kept_in_core INTEGER NOT NULL CHECK(kept_in_core IN (0, 1)),
            confidence REAL,
            alignment_model_id TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE(hypothesis_id, token_index),
            CHECK(session_end_ms > session_start_ms),
            CHECK(analysis_end_ms > analysis_start_ms),
            FOREIGN KEY (hypothesis_id) REFERENCES asr_hypotheses(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_asr_alignment_tokens_time
        ON asr_alignment_tokens(hypothesis_id, session_start_ms, session_end_ms);

        CREATE TABLE IF NOT EXISTS asr_token_sources (
            token_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            source_object_id INTEGER NOT NULL,
            source_sha256 TEXT NOT NULL,
            source_start_ms INTEGER NOT NULL,
            source_end_ms INTEGER NOT NULL,
            PRIMARY KEY (token_id, position),
            CHECK(source_end_ms > source_start_ms),
            FOREIGN KEY (token_id) REFERENCES asr_alignment_tokens(id) ON DELETE RESTRICT,
            FOREIGN KEY (source_object_id) REFERENCES source_objects(id) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS asr_disagreements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            window_index INTEGER NOT NULL,
            primary_hypothesis_id INTEGER NOT NULL,
            secondary_hypothesis_id INTEGER NOT NULL,
            session_start_ms INTEGER NOT NULL,
            session_end_ms INTEGER NOT NULL,
            normalized_distance REAL NOT NULL,
            priority TEXT NOT NULL CHECK(priority IN ('low', 'medium', 'high')),
            details_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, window_index),
            CHECK(session_end_ms > session_start_ms),
            CHECK(normalized_distance >= 0.0 AND normalized_distance <= 1.0),
            FOREIGN KEY (run_id) REFERENCES processing_runs(id) ON DELETE RESTRICT,
            FOREIGN KEY (primary_hypothesis_id) REFERENCES asr_hypotheses(id) ON DELETE RESTRICT,
            FOREIGN KEY (secondary_hypothesis_id) REFERENCES asr_hypotheses(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_asr_disagreements_run_priority
        ON asr_disagreements(run_id, priority, window_index);

        CREATE TRIGGER IF NOT EXISTS protect_asr_hypotheses_from_update
        BEFORE UPDATE ON asr_hypotheses BEGIN
            SELECT RAISE(ABORT, 'ASR hypothesis cannot be changed');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_asr_hypotheses_from_delete
        BEFORE DELETE ON asr_hypotheses BEGIN
            SELECT RAISE(ABORT, 'ASR hypothesis cannot be deleted');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_asr_tokens_from_update
        BEFORE UPDATE ON asr_alignment_tokens BEGIN
            SELECT RAISE(ABORT, 'ASR token cannot be changed');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_asr_tokens_from_delete
        BEFORE DELETE ON asr_alignment_tokens BEGIN
            SELECT RAISE(ABORT, 'ASR token cannot be deleted');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_asr_token_sources_from_update
        BEFORE UPDATE ON asr_token_sources BEGIN
            SELECT RAISE(ABORT, 'ASR token source cannot be changed');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_asr_token_sources_from_delete
        BEFORE DELETE ON asr_token_sources BEGIN
            SELECT RAISE(ABORT, 'ASR token source cannot be deleted');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_asr_disagreements_from_update
        BEFORE UPDATE ON asr_disagreements BEGIN
            SELECT RAISE(ABORT, 'ASR disagreement cannot be changed');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_asr_disagreements_from_delete
        BEFORE DELETE ON asr_disagreements BEGIN
            SELECT RAISE(ABORT, 'ASR disagreement cannot be deleted');
        END;
    """,
    7: """
        CREATE TABLE IF NOT EXISTS diarization_turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            turn_key TEXT NOT NULL UNIQUE,
            run_id INTEGER NOT NULL,
            session_id INTEGER NOT NULL,
            turn_index INTEGER NOT NULL,
            turn_kind TEXT NOT NULL CHECK(turn_kind IN ('regular', 'exclusive')),
            speaker_label TEXT NOT NULL,
            session_start_ms INTEGER NOT NULL,
            session_end_ms INTEGER NOT NULL,
            confidence REAL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            content_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, turn_kind, turn_index),
            CHECK(session_end_ms > session_start_ms),
            CHECK(confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
            FOREIGN KEY (run_id) REFERENCES processing_runs(id) ON DELETE RESTRICT,
            FOREIGN KEY (session_id) REFERENCES recording_sessions(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_diarization_turns_run_time
        ON diarization_turns(run_id, turn_kind, session_start_ms, session_end_ms);

        CREATE TABLE IF NOT EXISTS diarization_turn_sources (
            turn_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            source_object_id INTEGER NOT NULL,
            source_sha256 TEXT NOT NULL,
            source_start_ms INTEGER NOT NULL,
            source_end_ms INTEGER NOT NULL,
            PRIMARY KEY (turn_id, position),
            CHECK(source_end_ms > source_start_ms),
            FOREIGN KEY (turn_id) REFERENCES diarization_turns(id) ON DELETE RESTRICT,
            FOREIGN KEY (source_object_id) REFERENCES source_objects(id) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS token_speaker_attributions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attribution_key TEXT NOT NULL UNIQUE,
            run_id INTEGER NOT NULL,
            asr_run_id INTEGER NOT NULL,
            token_id INTEGER NOT NULL,
            speaker_label TEXT,
            attribution_kind TEXT NOT NULL CHECK(
                attribution_kind IN ('primary', 'overlap', 'uncertain', 'none')
            ),
            overlap_ms INTEGER NOT NULL DEFAULT 0,
            overlap_ratio REAL NOT NULL DEFAULT 0.0,
            rank INTEGER NOT NULL,
            confidence REAL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            content_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, token_id, rank),
            CHECK(overlap_ms >= 0),
            CHECK(overlap_ratio >= 0.0 AND overlap_ratio <= 1.0),
            CHECK(rank >= 0),
            CHECK(confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
            CHECK(
                (attribution_kind = 'none' AND speaker_label IS NULL)
                OR (attribution_kind != 'none' AND speaker_label IS NOT NULL)
            ),
            FOREIGN KEY (run_id) REFERENCES processing_runs(id) ON DELETE RESTRICT,
            FOREIGN KEY (asr_run_id) REFERENCES processing_runs(id) ON DELETE RESTRICT,
            FOREIGN KEY (token_id) REFERENCES asr_alignment_tokens(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_token_speaker_attributions_run_token
        ON token_speaker_attributions(run_id, token_id, rank);

        CREATE TRIGGER IF NOT EXISTS protect_diarization_turns_from_update
        BEFORE UPDATE ON diarization_turns BEGIN
            SELECT RAISE(ABORT, 'diarization turn cannot be changed');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_diarization_turns_from_delete
        BEFORE DELETE ON diarization_turns BEGIN
            SELECT RAISE(ABORT, 'diarization turn cannot be deleted');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_diarization_turn_sources_from_update
        BEFORE UPDATE ON diarization_turn_sources BEGIN
            SELECT RAISE(ABORT, 'diarization turn source cannot be changed');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_diarization_turn_sources_from_delete
        BEFORE DELETE ON diarization_turn_sources BEGIN
            SELECT RAISE(ABORT, 'diarization turn source cannot be deleted');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_token_speaker_attributions_from_update
        BEFORE UPDATE ON token_speaker_attributions BEGIN
            SELECT RAISE(ABORT, 'token speaker attribution cannot be changed');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_token_speaker_attributions_from_delete
        BEFORE DELETE ON token_speaker_attributions BEGIN
            SELECT RAISE(ABORT, 'token speaker attribution cannot be deleted');
        END;
    """,
    8: """
        CREATE TABLE IF NOT EXISTS identity_candidate_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            candidate_id TEXT NOT NULL,
            target_identity TEXT NOT NULL,
            session_start_ms INTEGER NOT NULL,
            session_end_ms INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(
                status IN ('confirmed_target', 'rejected', 'uncertain')
            ),
            note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(run_id, candidate_id),
            CHECK(session_end_ms > session_start_ms),
            FOREIGN KEY (run_id) REFERENCES processing_runs(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_identity_candidate_reviews_run_status
        ON identity_candidate_reviews(run_id, status, session_start_ms);
    """,
    9: """
        CREATE TABLE IF NOT EXISTS identity_reference_intervals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reference_key TEXT NOT NULL UNIQUE,
            identity_label TEXT NOT NULL,
            decision TEXT NOT NULL CHECK(
                decision IN ('confirmed_target', 'rejected', 'uncertain')
            ),
            session_id INTEGER NOT NULL,
            session_start_ms INTEGER NOT NULL,
            session_end_ms INTEGER NOT NULL,
            source_object_id INTEGER NOT NULL,
            source_sha256 TEXT NOT NULL,
            source_start_ms INTEGER NOT NULL,
            source_end_ms INTEGER NOT NULL,
            provenance_kind TEXT NOT NULL CHECK(
                provenance_kind IN ('truth', 'v2d3_review')
            ),
            provenance_id INTEGER NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK(session_end_ms > session_start_ms),
            CHECK(source_end_ms > source_start_ms),
            UNIQUE(
                identity_label, session_id, source_object_id,
                source_start_ms, source_end_ms
            ),
            FOREIGN KEY (session_id) REFERENCES recording_sessions(id) ON DELETE RESTRICT,
            FOREIGN KEY (source_object_id) REFERENCES source_objects(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_identity_reference_label_decision
        ON identity_reference_intervals(
            identity_label, decision, session_id, session_start_ms
        );
    """,
    10: """
        CREATE TABLE IF NOT EXISTS semantic_exchanges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL UNIQUE,
            session_id INTEGER NOT NULL,
            asr_run_id INTEGER NOT NULL,
            diarization_run_id INTEGER,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            request_format TEXT NOT NULL,
            response_format TEXT NOT NULL,
            request_json TEXT NOT NULL,
            request_sha256 TEXT NOT NULL,
            response_json TEXT NOT NULL,
            response_sha256 TEXT NOT NULL,
            audio_bytes_included INTEGER NOT NULL DEFAULT 0 CHECK(audio_bytes_included = 0),
            source_paths_included INTEGER NOT NULL DEFAULT 0 CHECK(source_paths_included = 0),
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES processing_runs(id) ON DELETE RESTRICT,
            FOREIGN KEY (session_id) REFERENCES recording_sessions(id) ON DELETE RESTRICT,
            FOREIGN KEY (asr_run_id) REFERENCES processing_runs(id) ON DELETE RESTRICT,
            FOREIGN KEY (diarization_run_id) REFERENCES processing_runs(id) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS semantic_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            candidate_key TEXT NOT NULL UNIQUE,
            candidate_type TEXT NOT NULL CHECK(
                candidate_type IN ('daily_summary', 'event', 'fact', 'action')
            ),
            session_start_ms INTEGER NOT NULL,
            session_end_ms INTEGER NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            confidence REAL,
            evidence_json TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, candidate_key),
            CHECK(session_end_ms > session_start_ms),
            CHECK(confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
            FOREIGN KEY (run_id) REFERENCES processing_runs(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_semantic_candidates_run_type_time
        ON semantic_candidates(run_id, candidate_type, session_start_ms);

        CREATE TABLE IF NOT EXISTS semantic_candidate_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL,
            revision_index INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('confirmed', 'rejected')),
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            note TEXT,
            content_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(candidate_id, revision_index),
            CHECK(revision_index >= 1),
            FOREIGN KEY (candidate_id) REFERENCES semantic_candidates(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_semantic_revisions_candidate_index
        ON semantic_candidate_revisions(candidate_id, revision_index DESC);

        CREATE TRIGGER IF NOT EXISTS protect_semantic_exchanges_from_update
        BEFORE UPDATE ON semantic_exchanges BEGIN
            SELECT RAISE(ABORT, 'semantic exchange cannot be changed');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_semantic_exchanges_from_delete
        BEFORE DELETE ON semantic_exchanges BEGIN
            SELECT RAISE(ABORT, 'semantic exchange cannot be deleted');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_semantic_candidates_from_update
        BEFORE UPDATE ON semantic_candidates BEGIN
            SELECT RAISE(ABORT, 'semantic candidate cannot be changed');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_semantic_candidates_from_delete
        BEFORE DELETE ON semantic_candidates BEGIN
            SELECT RAISE(ABORT, 'semantic candidate cannot be deleted');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_semantic_revisions_from_update
        BEFORE UPDATE ON semantic_candidate_revisions BEGIN
            SELECT RAISE(ABORT, 'semantic revision cannot be changed');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_semantic_revisions_from_delete
        BEFORE DELETE ON semantic_candidate_revisions BEGIN
            SELECT RAISE(ABORT, 'semantic revision cannot be deleted');
        END;
    """,
    11: """
        PRAGMA foreign_keys = OFF;
        BEGIN IMMEDIATE;

        CREATE TABLE source_instances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_key TEXT NOT NULL UNIQUE,
            source_object_id INTEGER NOT NULL,
            source_path TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            byte_size INTEGER NOT NULL,
            recorded_at TEXT NOT NULL,
            timezone TEXT NOT NULL,
            device TEXT,
            ingest_method TEXT NOT NULL CHECK(
                ingest_method IN ('manual_file', 'watch_auto', 'watch_manual_sync')
            ),
            sample_rate INTEGER,
            sample_count INTEGER,
            integrity_status TEXT NOT NULL DEFAULT 'unverified' CHECK(
                integrity_status IN ('unverified', 'verified', 'missing', 'mismatch', 'error')
            ),
            last_verified_at TEXT,
            backup_status TEXT NOT NULL DEFAULT 'not_configured' CHECK(
                backup_status IN ('not_configured', 'pending', 'verified', 'failed')
            ),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK(sample_rate IS NULL OR sample_rate > 0),
            CHECK(sample_count IS NULL OR sample_count > 0),
            FOREIGN KEY (source_object_id) REFERENCES source_objects(id) ON DELETE RESTRICT
        );

        CREATE INDEX idx_source_instances_object
        ON source_instances(source_object_id, id);

        CREATE INDEX idx_source_instances_integrity
        ON source_instances(integrity_status, id);

        INSERT INTO source_instances (
            instance_key, source_object_id, source_path, original_filename,
            byte_size, recorded_at, timezone, device, ingest_method,
            sample_rate, sample_count, integrity_status, last_verified_at,
            backup_status, created_at, updated_at
        )
        SELECT
            'legacy-session:' || ss.session_id || ':chunk:' || ss.chunk_index,
            so.id, so.source_path, so.original_filename, COALESCE(so.byte_size, 0),
            so.recorded_at, so.timezone, so.device, so.ingest_method,
            so.sample_rate,
            CASE
                WHEN so.sample_rate IS NOT NULL
                THEN CAST(ROUND(so.duration_ms * so.sample_rate / 1000.0) AS INTEGER)
                ELSE NULL
            END,
            so.integrity_status, so.last_verified_at, so.backup_status,
            ss.created_at, ss.created_at
        FROM session_sources ss
        JOIN source_objects so ON so.id = ss.source_object_id
        ORDER BY ss.session_id, ss.chunk_index;

        CREATE TABLE session_sources_v11 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            source_object_id INTEGER NOT NULL,
            source_instance_id INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            session_start_ms INTEGER NOT NULL,
            session_end_ms INTEGER NOT NULL,
            source_start_ms INTEGER NOT NULL DEFAULT 0,
            source_end_ms INTEGER NOT NULL,
            session_start_sample INTEGER,
            session_end_sample INTEGER,
            source_start_sample INTEGER,
            source_end_sample INTEGER,
            timeline_sample_rate INTEGER,
            continuity_status TEXT NOT NULL DEFAULT 'unchecked' CHECK(
                continuity_status IN ('single', 'unchecked', 'continuous', 'gap', 'overlap')
            ),
            created_at TEXT NOT NULL,
            UNIQUE(session_id, chunk_index),
            CHECK(session_end_ms > session_start_ms),
            CHECK(source_end_ms > source_start_ms),
            CHECK(timeline_sample_rate IS NULL OR timeline_sample_rate > 0),
            FOREIGN KEY (session_id) REFERENCES recording_sessions(id) ON DELETE RESTRICT,
            FOREIGN KEY (source_object_id) REFERENCES source_objects(id) ON DELETE RESTRICT,
            FOREIGN KEY (source_instance_id) REFERENCES source_instances(id) ON DELETE RESTRICT
        );

        INSERT INTO session_sources_v11 (
            id, session_id, source_object_id, source_instance_id, chunk_index,
            session_start_ms, session_end_ms, source_start_ms, source_end_ms,
            continuity_status, created_at
        )
        SELECT
            ss.id, ss.session_id, ss.source_object_id, si.id, ss.chunk_index,
            ss.session_start_ms, ss.session_end_ms,
            ss.source_start_ms, ss.source_end_ms,
            ss.continuity_status, ss.created_at
        FROM session_sources ss
        JOIN source_instances si
          ON si.instance_key = (
              'legacy-session:' || ss.session_id || ':chunk:' || ss.chunk_index
          );

        DROP TABLE session_sources;
        ALTER TABLE session_sources_v11 RENAME TO session_sources;

        CREATE INDEX idx_session_sources_time
        ON session_sources(session_id, session_start_ms, session_end_ms);

        CREATE INDEX idx_session_sources_instance
        ON session_sources(source_instance_id, session_id);

        CREATE TABLE session_manifests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL UNIQUE,
            manifest_format TEXT NOT NULL,
            manifest_path TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL UNIQUE,
            byte_size INTEGER NOT NULL,
            parser_version TEXT NOT NULL,
            summary_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES recording_sessions(id) ON DELETE RESTRICT
        );

        CREATE TABLE processing_runs_v11 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recording_id INTEGER,
            session_id INTEGER,
            run_kind TEXT NOT NULL,
            status TEXT NOT NULL,
            config_json TEXT NOT NULL,
            config_sha256 TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            error TEXT,
            summary_json TEXT,
            artifacts_json TEXT,
            input_fingerprint TEXT,
            model_manifest_json TEXT NOT NULL DEFAULT '{}',
            pipeline_version TEXT,
            code_version TEXT,
            parent_run_id INTEGER,
            CHECK(recording_id IS NOT NULL OR session_id IS NOT NULL),
            FOREIGN KEY (recording_id) REFERENCES recordings(id) ON DELETE SET NULL,
            FOREIGN KEY (session_id) REFERENCES recording_sessions(id) ON DELETE RESTRICT
        );

        INSERT INTO processing_runs_v11 (
            id, recording_id, session_id, run_kind, status, config_json,
            config_sha256, started_at, completed_at, error, summary_json,
            artifacts_json, input_fingerprint, model_manifest_json,
            pipeline_version, code_version, parent_run_id
        )
        SELECT
            id, recording_id, session_id, run_kind, status, config_json,
            config_sha256, started_at, completed_at, error, summary_json,
            artifacts_json, input_fingerprint, model_manifest_json,
            pipeline_version, code_version, parent_run_id
        FROM processing_runs;

        DROP TABLE processing_runs;
        ALTER TABLE processing_runs_v11 RENAME TO processing_runs;

        CREATE INDEX idx_processing_runs_recording
        ON processing_runs(recording_id, id);

        CREATE INDEX idx_processing_runs_session
        ON processing_runs(session_id, id);

        ALTER TABLE processing_run_inputs
        ADD COLUMN source_instance_id INTEGER REFERENCES source_instances(id);

        ALTER TABLE asr_token_sources
        ADD COLUMN source_instance_id INTEGER REFERENCES source_instances(id);

        ALTER TABLE diarization_turn_sources
        ADD COLUMN source_instance_id INTEGER REFERENCES source_instances(id);

        ALTER TABLE truth_annotation_sources
        ADD COLUMN source_instance_id INTEGER REFERENCES source_instances(id);

        CREATE TABLE identity_reference_intervals_v11 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reference_key TEXT NOT NULL UNIQUE,
            identity_label TEXT NOT NULL,
            decision TEXT NOT NULL CHECK(
                decision IN ('confirmed_target', 'rejected', 'uncertain')
            ),
            session_id INTEGER NOT NULL,
            session_start_ms INTEGER NOT NULL,
            session_end_ms INTEGER NOT NULL,
            source_object_id INTEGER NOT NULL,
            source_instance_id INTEGER NOT NULL,
            source_sha256 TEXT NOT NULL,
            source_start_ms INTEGER NOT NULL,
            source_end_ms INTEGER NOT NULL,
            provenance_kind TEXT NOT NULL CHECK(
                provenance_kind IN ('truth', 'v2d3_review')
            ),
            provenance_id INTEGER NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK(session_end_ms > session_start_ms),
            CHECK(source_end_ms > source_start_ms),
            UNIQUE(
                identity_label, session_id, source_instance_id,
                source_start_ms, source_end_ms
            ),
            FOREIGN KEY (session_id) REFERENCES recording_sessions(id) ON DELETE RESTRICT,
            FOREIGN KEY (source_object_id) REFERENCES source_objects(id) ON DELETE RESTRICT,
            FOREIGN KEY (source_instance_id) REFERENCES source_instances(id) ON DELETE RESTRICT
        );

        INSERT INTO identity_reference_intervals_v11 (
            id, reference_key, identity_label, decision, session_id,
            session_start_ms, session_end_ms, source_object_id,
            source_instance_id, source_sha256, source_start_ms, source_end_ms,
            provenance_kind, provenance_id, metadata_json, created_at, updated_at
        )
        SELECT
            id, reference_key, identity_label, decision, session_id,
            session_start_ms, session_end_ms, source_object_id,
            (
                SELECT ss.source_instance_id
                FROM session_sources ss
                WHERE ss.session_id = identity_reference_intervals.session_id
                  AND ss.source_object_id = identity_reference_intervals.source_object_id
                  AND identity_reference_intervals.source_start_ms >= ss.source_start_ms
                  AND identity_reference_intervals.source_end_ms <= ss.source_end_ms
                LIMIT 1
            ),
            source_sha256, source_start_ms, source_end_ms,
            provenance_kind, provenance_id, metadata_json, created_at, updated_at
        FROM identity_reference_intervals;

        DROP TABLE identity_reference_intervals;
        ALTER TABLE identity_reference_intervals_v11
        RENAME TO identity_reference_intervals;

        CREATE INDEX idx_identity_reference_label_decision
        ON identity_reference_intervals(
            identity_label, decision, session_id, session_start_ms
        );

        UPDATE processing_run_inputs
        SET source_instance_id = (
            SELECT ss.source_instance_id
            FROM processing_runs pr
            JOIN session_sources ss ON ss.session_id = pr.session_id
            WHERE pr.id = processing_run_inputs.run_id
              AND ss.source_object_id = processing_run_inputs.source_object_id
              AND ss.session_start_ms = processing_run_inputs.session_start_ms
              AND ss.session_end_ms = processing_run_inputs.session_end_ms
              AND ss.source_start_ms = processing_run_inputs.source_start_ms
              AND ss.source_end_ms = processing_run_inputs.source_end_ms
            LIMIT 1
        );

        UPDATE asr_token_sources
        SET source_instance_id = (
            SELECT ss.source_instance_id
            FROM asr_alignment_tokens token
            JOIN asr_hypotheses hypothesis ON hypothesis.id = token.hypothesis_id
            JOIN session_sources ss ON ss.session_id = hypothesis.session_id
            WHERE token.id = asr_token_sources.token_id
              AND ss.source_object_id = asr_token_sources.source_object_id
              AND asr_token_sources.source_start_ms >= ss.source_start_ms
              AND asr_token_sources.source_end_ms <= ss.source_end_ms
            LIMIT 1
        );

        UPDATE diarization_turn_sources
        SET source_instance_id = (
            SELECT ss.source_instance_id
            FROM diarization_turns turn
            JOIN session_sources ss ON ss.session_id = turn.session_id
            WHERE turn.id = diarization_turn_sources.turn_id
              AND ss.source_object_id = diarization_turn_sources.source_object_id
              AND diarization_turn_sources.source_start_ms >= ss.source_start_ms
              AND diarization_turn_sources.source_end_ms <= ss.source_end_ms
            LIMIT 1
        );

        UPDATE truth_annotation_sources
        SET source_instance_id = (
            SELECT ss.source_instance_id
            FROM truth_annotations annotation
            JOIN truth_sets truth_set ON truth_set.id = annotation.truth_set_id
            JOIN session_sources ss ON ss.session_id = truth_set.session_id
            WHERE annotation.id = truth_annotation_sources.annotation_id
              AND ss.source_object_id = truth_annotation_sources.source_object_id
              AND truth_annotation_sources.source_start_ms >= ss.source_start_ms
              AND truth_annotation_sources.source_end_ms <= ss.source_end_ms
            LIMIT 1
        );

        UPDATE identity_reference_intervals
        SET source_instance_id = (
            SELECT ss.source_instance_id
            FROM session_sources ss
            WHERE ss.session_id = identity_reference_intervals.session_id
              AND ss.source_object_id = identity_reference_intervals.source_object_id
              AND identity_reference_intervals.source_start_ms >= ss.source_start_ms
              AND identity_reference_intervals.source_end_ms <= ss.source_end_ms
            LIMIT 1
        );

        CREATE TRIGGER protect_source_instances_immutable_fields
        BEFORE UPDATE OF
            instance_key, source_object_id, source_path, original_filename,
            byte_size, recorded_at, timezone, device, ingest_method,
            sample_rate, sample_count, created_at
        ON source_instances
        BEGIN
            SELECT RAISE(ABORT, 'immutable source instance metadata cannot be changed');
        END;

        CREATE TRIGGER protect_source_instances_from_delete
        BEFORE DELETE ON source_instances
        BEGIN
            SELECT RAISE(ABORT, 'immutable source instance cannot be deleted');
        END;

        CREATE TRIGGER protect_session_sources_from_update
        BEFORE UPDATE ON session_sources
        BEGIN
            SELECT RAISE(ABORT, 'session source mapping cannot be changed');
        END;

        CREATE TRIGGER protect_session_sources_from_delete
        BEFORE DELETE ON session_sources
        BEGIN
            SELECT RAISE(ABORT, 'session source mapping cannot be deleted');
        END;

        CREATE TRIGGER protect_closed_session_from_source_insert
        BEFORE INSERT ON session_sources
        WHEN (SELECT status FROM recording_sessions WHERE id = NEW.session_id) != 'active'
        BEGIN
            SELECT RAISE(ABORT, 'cannot append source to a closed session');
        END;

        CREATE TRIGGER protect_closed_session_immutable_fields
        BEFORE UPDATE OF
            session_key, legacy_recording_id, device, recorded_at,
            timezone, duration_ms, status, created_at
        ON recording_sessions
        WHEN OLD.status = 'closed'
        BEGIN
            SELECT RAISE(ABORT, 'closed recording session cannot be changed');
        END;

        CREATE TRIGGER protect_recording_sessions_from_delete
        BEFORE DELETE ON recording_sessions
        BEGIN
            SELECT RAISE(ABORT, 'recording session cannot be deleted');
        END;

        CREATE TRIGGER protect_session_manifests_from_update
        BEFORE UPDATE ON session_manifests
        BEGIN
            SELECT RAISE(ABORT, 'session manifest cannot be changed');
        END;

        CREATE TRIGGER protect_session_manifests_from_delete
        BEFORE DELETE ON session_manifests
        BEGIN
            SELECT RAISE(ABORT, 'session manifest cannot be deleted');
        END;

        COMMIT;
        PRAGMA foreign_keys = ON;
    """,
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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_path(path: Path, block_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(block_size):
            digest.update(block)
    return digest.hexdigest()


def _canonical_source_fingerprint(rows: Sequence[sqlite3.Row]) -> str:
    payload = [
        {
            "position": position,
            "source_object_id": int(row["source_object_id"]),
            "source_instance_id": int(row["source_instance_id"]),
            "instance_key": str(row["instance_key"]),
            "sha256": str(row["sha256"]),
            "session_start_ms": int(row["session_start_ms"]),
            "session_end_ms": int(row["session_end_ms"]),
            "source_start_ms": int(row["source_start_ms"]),
            "source_end_ms": int(row["source_end_ms"]),
            "session_start_sample": row["session_start_sample"],
            "session_end_sample": row["session_end_sample"],
            "source_start_sample": row["source_start_sample"],
            "source_end_sample": row["source_end_sample"],
            "timeline_sample_rate": row["timeline_sample_rate"],
        }
        for position, row in enumerate(rows)
    ]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _ensure_v4_processing_run_columns(connection: sqlite3.Connection) -> None:
    existing = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(processing_runs)")
    }
    for name, declaration in V4_PROCESSING_RUN_COLUMNS.items():
        if name not in existing:
            connection.execute(
                f"ALTER TABLE processing_runs ADD COLUMN {name} {declaration}"
            )


def _ensure_v5_prediction_set_columns(connection: sqlite3.Connection) -> None:
    existing = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(benchmark_prediction_sets)")
    }
    for name, declaration in V5_PREDICTION_SET_COLUMNS.items():
        if name not in existing:
            connection.execute(
                f"ALTER TABLE benchmark_prediction_sets ADD COLUMN {name} {declaration}"
            )


def _backfill_v2_source_graph(
    connection: sqlite3.Connection, recording_id: int | None = None
) -> None:
    where = "WHERE id = ?" if recording_id is not None else ""
    params: tuple[Any, ...] = (recording_id,) if recording_id is not None else ()
    recordings = list(
        connection.execute(f"SELECT * FROM recordings {where} ORDER BY id", params)
    )
    now = utc_now()
    for recording in recordings:
        path = Path(str(recording["source_path"]))
        byte_size = path.stat().st_size if path.is_file() else None
        container = path.suffix.lower().lstrip(".") or None
        connection.execute(
            """
            INSERT INTO source_objects (
                sha256, source_path, original_filename, byte_size, container,
                codec, sample_rate, channels, bit_rate, encoder, device,
                recorded_at, timezone, duration_ms, ingest_method,
                storage_class, integrity_status, backup_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual_file',
                      'original_permanent', 'unverified', 'not_configured', ?, ?)
            ON CONFLICT(sha256) DO NOTHING
            """,
            (
                recording["sha256"],
                recording["source_path"],
                path.name,
                byte_size,
                container,
                recording["codec"],
                recording["sample_rate"],
                recording["channels"],
                recording["bit_rate"],
                recording["encoder"],
                recording["device"],
                recording["recorded_at"],
                recording["timezone"],
                recording["duration_ms"],
                recording["created_at"] or now,
                now,
            ),
        )
        source = connection.execute(
            "SELECT * FROM source_objects WHERE sha256 = ?", (recording["sha256"],)
        ).fetchone()
        session_key = f"legacy-recording:{int(recording['id'])}"
        connection.execute(
            """
            INSERT INTO recording_sessions (
                session_key, legacy_recording_id, device, recorded_at, timezone,
                duration_ms, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
            ON CONFLICT(session_key) DO NOTHING
            """,
            (
                session_key,
                recording["id"],
                recording["device"],
                recording["recorded_at"],
                recording["timezone"],
                recording["duration_ms"],
                recording["created_at"] or now,
                now,
            ),
        )
        session = connection.execute(
            "SELECT * FROM recording_sessions WHERE session_key = ?", (session_key,)
        ).fetchone()
        existing_mapping = connection.execute(
            """
            SELECT id, source_instance_id FROM session_sources
            WHERE session_id = ? AND chunk_index = 0
            """,
            (session["id"],),
        ).fetchone()
        if existing_mapping is None:
            instance_key = f"legacy-recording:{int(recording['id'])}"
            connection.execute(
                """
                INSERT INTO source_instances (
                    instance_key, source_object_id, source_path, original_filename,
                    byte_size, recorded_at, timezone, device, ingest_method,
                    sample_rate, sample_count, integrity_status, last_verified_at,
                    backup_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'manual_file', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instance_key) DO NOTHING
                """,
                (
                    instance_key,
                    source["id"],
                    recording["source_path"],
                    path.name,
                    byte_size or 0,
                    recording["recorded_at"],
                    recording["timezone"],
                    recording["device"],
                    recording["sample_rate"],
                    (
                        round(
                            int(recording["duration_ms"])
                            * int(recording["sample_rate"])
                            / 1000
                        )
                        if recording["sample_rate"] is not None
                        else None
                    ),
                    source["integrity_status"],
                    source["last_verified_at"],
                    source["backup_status"],
                    recording["created_at"] or now,
                    now,
                ),
            )
            source_instance = connection.execute(
                "SELECT * FROM source_instances WHERE instance_key = ?",
                (instance_key,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO session_sources (
                    session_id, source_object_id, source_instance_id,
                    chunk_index, session_start_ms, session_end_ms,
                    source_start_ms, source_end_ms, continuity_status, created_at
                ) VALUES (?, ?, ?, 0, 0, ?, 0, ?, 'single', ?)
                """,
                (
                    session["id"],
                    source["id"],
                    source_instance["id"],
                    recording["duration_ms"],
                    recording["duration_ms"],
                    now,
                ),
            )
        if str(session["status"]) == "active":
            connection.execute(
                """
                UPDATE recording_sessions
                SET status = 'closed', updated_at = ?
                WHERE id = ?
                """,
                (now, session["id"]),
            )

    runs = list(
        connection.execute(
            """
            SELECT pr.id AS run_id, rs.id AS session_id
            FROM processing_runs pr
            JOIN recording_sessions rs ON rs.legacy_recording_id = pr.recording_id
            ORDER BY pr.id
            """
        )
    )
    for run in runs:
        inputs = list(
            connection.execute(
                """
                SELECT ss.*, so.sha256, si.instance_key
                FROM session_sources ss
                JOIN source_objects so ON so.id = ss.source_object_id
                JOIN source_instances si ON si.id = ss.source_instance_id
                WHERE ss.session_id = ?
                ORDER BY ss.chunk_index
                """,
                (run["session_id"],),
            )
        )
        if not inputs:
            continue
        fingerprint = _canonical_source_fingerprint(inputs)
        connection.execute(
            """
            UPDATE processing_runs
            SET input_fingerprint = COALESCE(input_fingerprint, ?),
                session_id = COALESCE(session_id, ?)
            WHERE id = ?
            """,
            (fingerprint, run["session_id"], run["run_id"]),
        )
        connection.executemany(
            """
            INSERT INTO processing_run_inputs (
                run_id, position, source_object_id, source_instance_id, source_sha256,
                session_start_ms, session_end_ms, source_start_ms, source_end_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, position) DO NOTHING
            """,
            [
                (
                    run["run_id"],
                    position,
                    row["source_object_id"],
                    row["source_instance_id"],
                    row["sha256"],
                    row["session_start_ms"],
                    row["session_end_ms"],
                    row["source_start_ms"],
                    row["source_end_ms"],
                )
                for position, row in enumerate(inputs)
            ],
        )


class Database:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        previous_version = self._existing_schema_version()
        if 0 < previous_version < LATEST_SCHEMA_VERSION:
            self._backup_before_migration(previous_version)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            versions = {
                int(row["version"])
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            if not versions:
                connection.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (1, ?)",
                    (utc_now(),),
                )
                versions.add(1)
            if max(versions) > LATEST_SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库版本 {max(versions)} 高于程序支持的 {LATEST_SCHEMA_VERSION}"
                )
            for version in range(2, LATEST_SCHEMA_VERSION + 1):
                if version in versions:
                    continue
                if version == 11:
                    # V4 columns were historically added by an idempotent helper
                    # after the migration loop. V11 rebuilds processing_runs, so a
                    # database upgrading across several versions needs them first.
                    _ensure_v4_processing_run_columns(connection)
                connection.executescript(MIGRATIONS[version])
                connection.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, utc_now()),
                )
            _ensure_v4_processing_run_columns(connection)
            _ensure_v5_prediction_set_columns(connection)
            connection.executescript(V5_GUARD_SQL)
            connection.executescript(V7_GUARD_SQL)
            _backfill_v2_source_graph(connection)
            if previous_version < LATEST_SCHEMA_VERSION:
                connection.execute("PRAGMA optimize")

    def _existing_schema_version(self) -> int:
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return 0
        connection = sqlite3.connect(self.path)
        try:
            table = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'schema_migrations'
                """
            ).fetchone()
            if table is None:
                return 0
            row = connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()
            return int(row[0] or 0)
        finally:
            connection.close()

    def _backup_before_migration(self, previous_version: int) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = self.path.with_name(
            f"{self.path.stem}.schema-v{previous_version}-to-v{LATEST_SCHEMA_VERSION}."
            f"{timestamp}{self.path.suffix}"
        )
        source = sqlite3.connect(self.path)
        destination = sqlite3.connect(backup_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        return backup_path

    def schema_version(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT MAX(version) AS version FROM schema_migrations"
            ).fetchone()
            return int(row["version"] or 0)

    def find_recording_by_hash(self, sha256: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM recordings WHERE sha256 = ?", (sha256,)
            ).fetchone()

    def create_recording(self, values: dict[str, Any]) -> sqlite3.Row:
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO recordings (
                    source_path, sha256, device, recorded_at, timezone, duration_ms,
                    codec, sample_rate, channels, bit_rate, encoder, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["source_path"],
                    values["sha256"],
                    values.get("device"),
                    values["recorded_at"],
                    values["timezone"],
                    values["duration_ms"],
                    values.get("codec"),
                    values.get("sample_rate"),
                    values.get("channels"),
                    values.get("bit_rate"),
                    values.get("encoder"),
                    now,
                    now,
                ),
            )
            recording_id = int(cursor.lastrowid)
        with self.connect() as connection:
            _backfill_v2_source_graph(connection, recording_id=recording_id)
        return self.get_recording(recording_id)

    def get_recording(self, recording_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM recordings WHERE id = ?", (recording_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"录音 {recording_id} 不存在")
        return row

    def list_recordings(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(connection.execute("SELECT * FROM recordings ORDER BY id DESC"))

    def get_source_object(self, source_object_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM source_objects WHERE id = ?", (source_object_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"原始音频对象 {source_object_id} 不存在")
        return row

    def list_source_objects(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(connection.execute("SELECT * FROM source_objects ORDER BY id"))

    def get_source_instance(self, source_instance_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM source_instances WHERE id = ?", (source_instance_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"原始音频实例 {source_instance_id} 不存在")
        return row

    def list_source_instances(self, session_id: int | None = None) -> list[sqlite3.Row]:
        with self.connect() as connection:
            if session_id is None:
                return list(
                    connection.execute(
                        """
                        SELECT si.*, so.sha256, so.codec, so.channels, so.container
                        FROM source_instances si
                        JOIN source_objects so ON so.id = si.source_object_id
                        ORDER BY si.id
                        """
                    )
                )
            return list(
                connection.execute(
                    """
                    SELECT si.*, so.sha256, so.codec, so.channels, so.container,
                           ss.chunk_index, ss.session_start_ms, ss.session_end_ms,
                           ss.continuity_status
                    FROM session_sources ss
                    JOIN source_instances si ON si.id = ss.source_instance_id
                    JOIN source_objects so ON so.id = si.source_object_id
                    WHERE ss.session_id = ?
                    ORDER BY ss.chunk_index
                    """,
                    (session_id,),
                )
            )

    def update_source_instance_integrity(
        self,
        source_instance_id: int,
        *,
        status: str,
        verified_at: str | None,
    ) -> None:
        if status not in {"unverified", "verified", "missing", "mismatch", "error"}:
            raise ValueError(f"无效的原始音频实例完整性状态：{status}")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE source_instances
                SET integrity_status = ?, last_verified_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, verified_at, utc_now(), source_instance_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"原始音频实例 {source_instance_id} 不存在")

    def create_source_object(self, values: dict[str, Any]) -> sqlite3.Row:
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM source_objects WHERE sha256 = ?", (values["sha256"],)
            ).fetchone()
            if existing is not None:
                return existing
            now = utc_now()
            cursor = connection.execute(
                """
                INSERT INTO source_objects (
                    sha256, source_path, original_filename, byte_size, container,
                    codec, sample_rate, channels, bit_rate, encoder, device,
                    recorded_at, timezone, duration_ms, ingest_method,
                    storage_class, integrity_status, backup_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          'original_permanent', 'unverified', 'not_configured', ?, ?)
                """,
                (
                    values["sha256"],
                    values["source_path"],
                    values.get("original_filename")
                    or Path(values["source_path"]).name,
                    values.get("byte_size"),
                    values.get("container"),
                    values.get("codec"),
                    values.get("sample_rate"),
                    values.get("channels"),
                    values.get("bit_rate"),
                    values.get("encoder"),
                    values.get("device"),
                    values["recorded_at"],
                    values["timezone"],
                    values["duration_ms"],
                    values.get("ingest_method", "manual_file"),
                    now,
                    now,
                ),
            )
            source_object_id = int(cursor.lastrowid)
            return connection.execute(
                "SELECT * FROM source_objects WHERE id = ?", (source_object_id,)
            ).fetchone()

    def get_recording_session(self, session_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM recording_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"录音会话 {session_id} 不存在")
        return row

    def find_recording_session(self, session_key: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM recording_sessions WHERE session_key = ?", (session_key,)
            ).fetchone()

    def get_session_manifest(self, session_id: int) -> sqlite3.Row | None:
        self.get_recording_session(session_id)
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM session_manifests WHERE session_id = ?", (session_id,)
            ).fetchone()

    def find_session_manifest_by_hash(self, manifest_sha256: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM session_manifests WHERE manifest_sha256 = ?",
                (manifest_sha256,),
            ).fetchone()

    def get_session_for_recording(self, recording_id: int) -> sqlite3.Row:
        self.get_recording(recording_id)
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM recording_sessions
                WHERE legacy_recording_id = ?
                """,
                (recording_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"录音 {recording_id} 尚未映射到 V2 会话")
        return row

    def list_recording_sessions(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(connection.execute("SELECT * FROM recording_sessions ORDER BY id"))

    def create_recording_session(self, values: dict[str, Any]) -> sqlite3.Row:
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO recording_sessions (
                    session_key, legacy_recording_id, device, recorded_at,
                    timezone, duration_ms, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["session_key"],
                    values.get("legacy_recording_id"),
                    values.get("device"),
                    values["recorded_at"],
                    values["timezone"],
                    values["duration_ms"],
                    values.get("status", "active"),
                    now,
                    now,
                ),
            )
            session_id = int(cursor.lastrowid)
            return connection.execute(
                "SELECT * FROM recording_sessions WHERE id = ?", (session_id,)
            ).fetchone()

    def add_session_source(self, values: dict[str, Any]) -> sqlite3.Row:
        session = self.get_recording_session(int(values["session_id"]))
        if str(session["status"]) != "active":
            raise ValueError("关闭的录音会话不能追加原始音频")
        source = self.get_source_object(int(values["source_object_id"]))
        created_at = utc_now()
        with self.connect() as connection:
            source_instance_id = values.get("source_instance_id")
            if source_instance_id is None:
                instance_key = str(
                    values.get("instance_key")
                    or (
                        f"manual-session:{int(values['session_id'])}:"
                        f"chunk:{int(values['chunk_index'])}"
                    )
                )
                existing_instance = connection.execute(
                    "SELECT * FROM source_instances WHERE instance_key = ?",
                    (instance_key,),
                ).fetchone()
                if existing_instance is None:
                    cursor = connection.execute(
                        """
                        INSERT INTO source_instances (
                            instance_key, source_object_id, source_path,
                            original_filename, byte_size, recorded_at, timezone,
                            device, ingest_method, sample_rate, sample_count,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            instance_key,
                            source["id"],
                            source["source_path"],
                            source["original_filename"],
                            int(source["byte_size"] or 0),
                            source["recorded_at"],
                            source["timezone"],
                            source["device"],
                            source["ingest_method"],
                            source["sample_rate"],
                            (
                                round(
                                    int(source["duration_ms"])
                                    * int(source["sample_rate"])
                                    / 1000
                                )
                                if source["sample_rate"] is not None
                                else None
                            ),
                            created_at,
                            created_at,
                        ),
                    )
                    source_instance_id = int(cursor.lastrowid)
                else:
                    source_instance_id = int(existing_instance["id"])
            else:
                instance = connection.execute(
                    "SELECT * FROM source_instances WHERE id = ?",
                    (source_instance_id,),
                ).fetchone()
                if instance is None:
                    raise KeyError(f"原始音频实例 {source_instance_id} 不存在")
                if int(instance["source_object_id"]) != int(source["id"]):
                    raise ValueError("原始音频实例与内容对象不匹配")
            cursor = connection.execute(
                """
                INSERT INTO session_sources (
                    session_id, source_object_id, source_instance_id, chunk_index,
                    session_start_ms, session_end_ms, source_start_ms,
                    source_end_ms, session_start_sample, session_end_sample,
                    source_start_sample, source_end_sample, timeline_sample_rate,
                    continuity_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["session_id"],
                    values["source_object_id"],
                    source_instance_id,
                    values["chunk_index"],
                    values["session_start_ms"],
                    values["session_end_ms"],
                    values.get("source_start_ms", 0),
                    values["source_end_ms"],
                    values.get("session_start_sample"),
                    values.get("session_end_sample"),
                    values.get("source_start_sample"),
                    values.get("source_end_sample"),
                    values.get("timeline_sample_rate"),
                    values.get("continuity_status", "unchecked"),
                    created_at,
                ),
            )
            source_id = int(cursor.lastrowid)
            connection.execute(
                """
                UPDATE recording_sessions
                SET duration_ms = MAX(duration_ms, ?), updated_at = ?
                WHERE id = ?
                """,
                (values["session_end_ms"], created_at, values["session_id"]),
            )
            return connection.execute(
                "SELECT * FROM session_sources WHERE id = ?", (source_id,)
            ).fetchone()

    def list_session_sources(self, session_id: int) -> list[sqlite3.Row]:
        self.get_recording_session(session_id)
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT ss.*, so.sha256,
                           si.source_path AS source_path,
                           si.original_filename AS original_filename,
                           si.byte_size AS instance_byte_size,
                           si.instance_key,
                           si.integrity_status,
                           so.duration_ms AS source_duration_ms
                    FROM session_sources ss
                    JOIN source_objects so ON so.id = ss.source_object_id
                    JOIN source_instances si ON si.id = ss.source_instance_id
                    WHERE ss.session_id = ?
                    ORDER BY ss.chunk_index
                    """,
                    (session_id,),
                )
            )

    def session_input_fingerprint(self, session_id: int) -> str:
        rows = self.list_session_sources(session_id)
        if not rows:
            raise RuntimeError(f"录音会话 {session_id} 没有原始音频对象")
        return _canonical_source_fingerprint(rows)

    def import_closed_session(
        self,
        session_values: dict[str, Any],
        manifest_values: dict[str, Any],
        chunks: Sequence[dict[str, Any]],
    ) -> tuple[sqlite3.Row, bool]:
        """Atomically register a validated immutable manifest and all source instances."""
        if not chunks:
            raise ValueError("录音会话清单没有音频分片")
        session_key = str(session_values["session_key"])
        manifest_sha256 = str(manifest_values["manifest_sha256"])
        now = utc_now()
        with self.connect() as connection:
            existing_manifest = connection.execute(
                """
                SELECT sm.*, rs.session_key
                FROM session_manifests sm
                JOIN recording_sessions rs ON rs.id = sm.session_id
                WHERE sm.manifest_sha256 = ?
                """,
                (manifest_sha256,),
            ).fetchone()
            if existing_manifest is not None:
                if str(existing_manifest["session_key"]) != session_key:
                    raise ValueError("相同清单哈希已经属于另一个录音会话")
                session = connection.execute(
                    "SELECT * FROM recording_sessions WHERE id = ?",
                    (existing_manifest["session_id"],),
                ).fetchone()
                return session, False

            existing_session = connection.execute(
                "SELECT * FROM recording_sessions WHERE session_key = ?",
                (session_key,),
            ).fetchone()
            if existing_session is not None:
                raise ValueError("录音会话键已存在，但清单哈希不同")

            cursor = connection.execute(
                """
                INSERT INTO recording_sessions (
                    session_key, legacy_recording_id, device, recorded_at,
                    timezone, duration_ms, status, created_at, updated_at
                ) VALUES (?, NULL, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    session_key,
                    session_values.get("device"),
                    session_values["recorded_at"],
                    session_values["timezone"],
                    session_values["duration_ms"],
                    now,
                    now,
                ),
            )
            session_id = int(cursor.lastrowid)

            for chunk in chunks:
                source_values = dict(chunk["source"])
                instance_values = dict(chunk["instance"])
                mapping = dict(chunk["mapping"])
                source = connection.execute(
                    "SELECT * FROM source_objects WHERE sha256 = ?",
                    (source_values["sha256"],),
                ).fetchone()
                if source is None:
                    source_cursor = connection.execute(
                        """
                        INSERT INTO source_objects (
                            sha256, source_path, original_filename, byte_size,
                            container, codec, sample_rate, channels, bit_rate,
                            encoder, device, recorded_at, timezone, duration_ms,
                            ingest_method, storage_class, integrity_status,
                            last_verified_at, backup_status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  'original_permanent', 'verified', ?,
                                  'not_configured', ?, ?)
                        """,
                        (
                            source_values["sha256"],
                            source_values["source_path"],
                            source_values["original_filename"],
                            source_values["byte_size"],
                            source_values.get("container"),
                            source_values.get("codec"),
                            source_values.get("sample_rate"),
                            source_values.get("channels"),
                            source_values.get("bit_rate"),
                            source_values.get("encoder"),
                            source_values.get("device"),
                            source_values["recorded_at"],
                            source_values["timezone"],
                            source_values["duration_ms"],
                            source_values.get("ingest_method", "watch_manual_sync"),
                            now,
                            now,
                            now,
                        ),
                    )
                    source = connection.execute(
                        "SELECT * FROM source_objects WHERE id = ?",
                        (int(source_cursor.lastrowid),),
                    ).fetchone()
                instance_key = str(instance_values["instance_key"])
                if connection.execute(
                    "SELECT 1 FROM source_instances WHERE instance_key = ?",
                    (instance_key,),
                ).fetchone() is not None:
                    raise ValueError(f"原始音频实例键重复：{instance_key}")
                instance_cursor = connection.execute(
                    """
                    INSERT INTO source_instances (
                        instance_key, source_object_id, source_path,
                        original_filename, byte_size, recorded_at, timezone,
                        device, ingest_method, sample_rate, sample_count,
                        integrity_status, last_verified_at, backup_status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'verified', ?,
                              'not_configured', ?, ?)
                    """,
                    (
                        instance_key,
                        source["id"],
                        instance_values["source_path"],
                        instance_values["original_filename"],
                        instance_values["byte_size"],
                        instance_values["recorded_at"],
                        instance_values["timezone"],
                        instance_values.get("device"),
                        instance_values.get("ingest_method", "watch_manual_sync"),
                        instance_values.get("sample_rate"),
                        instance_values.get("sample_count"),
                        now,
                        now,
                        now,
                    ),
                )
                source_instance_id = int(instance_cursor.lastrowid)
                connection.execute(
                    """
                    INSERT INTO session_sources (
                        session_id, source_object_id, source_instance_id,
                        chunk_index, session_start_ms, session_end_ms,
                        source_start_ms, source_end_ms,
                        session_start_sample, session_end_sample,
                        source_start_sample, source_end_sample,
                        timeline_sample_rate, continuity_status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        source["id"],
                        source_instance_id,
                        mapping["chunk_index"],
                        mapping["session_start_ms"],
                        mapping["session_end_ms"],
                        mapping.get("source_start_ms", 0),
                        mapping["source_end_ms"],
                        mapping.get("session_start_sample"),
                        mapping.get("session_end_sample"),
                        mapping.get("source_start_sample", 0),
                        mapping.get("source_end_sample"),
                        mapping.get("timeline_sample_rate"),
                        mapping.get("continuity_status", "unchecked"),
                        now,
                    ),
                )

            connection.execute(
                """
                INSERT INTO session_manifests (
                    session_id, manifest_format, manifest_path,
                    manifest_sha256, byte_size, parser_version,
                    summary_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    manifest_values["manifest_format"],
                    manifest_values["manifest_path"],
                    manifest_sha256,
                    manifest_values["byte_size"],
                    manifest_values["parser_version"],
                    json.dumps(
                        manifest_values.get("summary", {}),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE recording_sessions
                SET status = 'closed', updated_at = ?
                WHERE id = ?
                """,
                (now, session_id),
            )
            session = connection.execute(
                "SELECT * FROM recording_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            return session, True

    def record_source_integrity_audit(
        self,
        source_object_id: int,
        *,
        status: str,
        actual_sha256: str | None,
        actual_byte_size: int | None,
        details: dict[str, Any],
    ) -> int:
        source = self.get_source_object(source_object_id)
        checked_at = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO source_integrity_audits (
                    source_object_id, status, expected_sha256, actual_sha256,
                    expected_byte_size, actual_byte_size, details_json, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_object_id,
                    status,
                    source["sha256"],
                    actual_sha256,
                    source["byte_size"],
                    actual_byte_size,
                    json.dumps(details, ensure_ascii=False, sort_keys=True),
                    checked_at,
                ),
            )
            connection.execute(
                """
                UPDATE source_objects
                SET integrity_status = ?, last_verified_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, checked_at, checked_at, source_object_id),
            )
            return int(cursor.lastrowid)

    def list_source_integrity_audits(self, source_object_id: int) -> list[sqlite3.Row]:
        self.get_source_object(source_object_id)
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM source_integrity_audits
                    WHERE source_object_id = ? ORDER BY id
                    """,
                    (source_object_id,),
                )
            )

    def update_recording(self, recording_id: int, **values: Any) -> None:
        if not values:
            return
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{column} = ?" for column in values)
        params = [*values.values(), recording_id]
        with self.connect() as connection:
            connection.execute(
                f"UPDATE recordings SET {assignments} WHERE id = ?", params
            )

    def set_stage(
        self,
        recording_id: int,
        stage: str,
        status: str,
        *,
        model_id: str | None = None,
        model_version: str | None = None,
        error: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now()
        started_at = now if status == "running" else None
        completed_at = now if status in {"completed", "partial", "failed"} else None
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT started_at FROM processing_stages WHERE recording_id = ? AND stage = ?",
                (recording_id, stage),
            ).fetchone()
            preserved_start = existing["started_at"] if existing else None
            connection.execute(
                """
                INSERT INTO processing_stages (
                    recording_id, stage, status, model_id, model_version,
                    started_at, completed_at, error, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(recording_id, stage) DO UPDATE SET
                    status = excluded.status,
                    model_id = COALESCE(excluded.model_id, processing_stages.model_id),
                    model_version = COALESCE(excluded.model_version, processing_stages.model_version),
                    started_at = COALESCE(excluded.started_at, processing_stages.started_at),
                    completed_at = excluded.completed_at,
                    error = excluded.error,
                    details_json = excluded.details_json
                """,
                (
                    recording_id,
                    stage,
                    status,
                    model_id,
                    model_version,
                    started_at or preserved_start,
                    completed_at,
                    error,
                    json.dumps(details, ensure_ascii=False) if details else None,
                ),
            )

    def get_stage(self, recording_id: int, stage: str) -> sqlite3.Row | None:
        """Return the persisted status for one processing stage, if present."""
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT * FROM processing_stages
                WHERE recording_id = ? AND stage = ?
                """,
                (recording_id, stage),
            ).fetchone()

    def list_stages(self, recording_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM processing_stages
                    WHERE recording_id = ? ORDER BY stage
                    """,
                    (recording_id,),
                )
            )

    def start_processing_run(
        self,
        recording_id: int | None,
        *,
        session_id: int | None = None,
        run_kind: str,
        config: dict[str, Any],
        config_sha256: str,
        model_manifest: dict[str, Any] | None = None,
        pipeline_version: str | None = None,
        code_version: str | None = None,
        parent_run_id: int | None = None,
    ) -> int:
        if recording_id is None and session_id is None:
            raise ValueError("处理运行必须指定 recording_id 或 session_id")
        if recording_id is not None:
            self.get_recording(recording_id)
        if session_id is None:
            session = self.get_session_for_recording(int(recording_id))
            session_id = int(session["id"])
        else:
            session = self.get_recording_session(session_id)
            legacy_recording_id = session["legacy_recording_id"]
            if (
                recording_id is not None
                and legacy_recording_id is not None
                and int(legacy_recording_id) != recording_id
            ):
                raise ValueError("recording_id 与 session_id 不属于同一录音会话")
        inputs = self.list_session_sources(int(session["id"]))
        input_fingerprint = _canonical_source_fingerprint(inputs)
        model_manifest = model_manifest or {}
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO processing_runs (
                    recording_id, session_id, run_kind, status, config_json,
                    config_sha256, started_at, input_fingerprint,
                    model_manifest_json, pipeline_version, code_version, parent_run_id
                ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recording_id,
                    session_id,
                    run_kind,
                    json.dumps(config, ensure_ascii=False, sort_keys=True),
                    config_sha256,
                    utc_now(),
                    input_fingerprint,
                    json.dumps(model_manifest, ensure_ascii=False, sort_keys=True),
                    pipeline_version,
                    code_version,
                    parent_run_id,
                ),
            )
            run_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO processing_run_inputs (
                    run_id, position, source_object_id, source_instance_id,
                    source_sha256,
                    session_start_ms, session_end_ms, source_start_ms, source_end_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        position,
                        row["source_object_id"],
                        row["source_instance_id"],
                        row["sha256"],
                        row["session_start_ms"],
                        row["session_end_ms"],
                        row["source_start_ms"],
                        row["source_end_ms"],
                    )
                    for position, row in enumerate(inputs)
                ],
            )
            return run_id

    def finish_processing_run(
        self,
        run_id: int,
        *,
        status: str,
        summary: dict[str, Any] | None = None,
        artifacts: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE processing_runs
                SET status = ?, completed_at = ?, error = ?,
                    summary_json = ?, artifacts_json = ?
                WHERE id = ?
                """,
                (
                    status,
                    utc_now(),
                    error[:2000] if error else None,
                    json.dumps(summary, ensure_ascii=False) if summary is not None else None,
                    json.dumps(artifacts, ensure_ascii=False) if artifacts is not None else None,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"处理运行 {run_id} 不存在")

    def update_processing_run_progress(
        self, run_id: int, summary: dict[str, Any]
    ) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE processing_runs
                SET summary_json = ?
                WHERE id = ? AND status = 'running'
                """,
                (json.dumps(summary, ensure_ascii=False, sort_keys=True), run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("只能更新运行中的处理任务进度")

    def resume_processing_run(self, run_id: int) -> sqlite3.Row:
        run = self.get_processing_run(run_id)
        if str(run["run_kind"]) != "quality_asr_v2c":
            raise ValueError("only V2-C ASR runs can be resumed by this command")
        if str(run["status"]) == "completed":
            return run
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE processing_runs
                SET status = 'running', completed_at = NULL, error = NULL
                WHERE id = ?
                """,
                (run_id,),
            )
        return self.get_processing_run(run_id)

    def list_processing_runs(self, recording_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM processing_runs
                    WHERE recording_id = ? ORDER BY id
                    """,
                    (recording_id,),
                )
            )

    def list_session_processing_runs(self, session_id: int) -> list[sqlite3.Row]:
        self.get_recording_session(session_id)
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM processing_runs
                    WHERE session_id = ? ORDER BY id
                    """,
                    (session_id,),
                )
            )

    def create_semantic_snapshot(
        self,
        run_id: int,
        exchange: dict[str, Any],
        candidates: Sequence[dict[str, Any]],
    ) -> list[sqlite3.Row]:
        """Append one private, immutable V2-E.0 exchange and its candidates."""
        run = self.get_processing_run(run_id)
        if str(run["run_kind"]) != "semantic_v2e0":
            raise ValueError("语义快照只适用于 V2-E.0 run")
        if str(run["status"]) != "running":
            raise ValueError("语义快照只能写入运行中的 V2-E.0 run")
        session_id = int(run["session_id"])
        asr_run_id = int(exchange["asr_run_id"])
        asr_run = self.get_processing_run(asr_run_id)
        if (
            str(asr_run["run_kind"]) != "quality_asr_v2c"
            or str(asr_run["status"]) != "completed"
            or int(asr_run["session_id"]) != session_id
        ):
            raise ValueError("语义快照需要同会话已完成的 V2-C ASR run")
        diarization_run_id = exchange.get("diarization_run_id")
        if diarization_run_id is not None:
            diarization_run = self.get_processing_run(int(diarization_run_id))
            if (
                str(diarization_run["run_kind"]) != "quality_diarization_v2d"
                or str(diarization_run["status"]) != "completed"
                or int(diarization_run["session_id"]) != session_id
            ):
                raise ValueError("语义快照的 V2-D run 无效")
        if exchange.get("audio_bytes_included") or exchange.get(
            "source_paths_included"
        ):
            raise ValueError("V2-E.0 禁止在语义交换中包含音频字节或原音路径")

        request_json = json.dumps(
            exchange["request"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        response_json = json.dumps(
            exchange["response"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        request_sha256 = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        response_sha256 = hashlib.sha256(response_json.encode("utf-8")).hexdigest()
        if exchange.get("request_sha256") not in {None, request_sha256}:
            raise ValueError("语义请求 SHA-256 不正确")
        if exchange.get("response_sha256") not in {None, response_sha256}:
            raise ValueError("语义响应 SHA-256 不正确")

        session = self.get_recording_session(session_id)
        duration_ms = int(session["duration_ms"])
        prepared: list[tuple[Any, ...]] = []
        for candidate in candidates:
            candidate_type = str(candidate["candidate_type"])
            if candidate_type not in {"daily_summary", "event", "fact", "action"}:
                raise ValueError("语义候选类型无效")
            start_ms = int(candidate["session_start_ms"])
            end_ms = int(candidate["session_end_ms"])
            if start_ms < 0 or end_ms <= start_ms or end_ms > duration_ms:
                raise ValueError("语义候选超出录音会话范围")
            confidence = candidate.get("confidence")
            if confidence is not None and not 0.0 <= float(confidence) <= 1.0:
                raise ValueError("语义候选置信度无效")
            title = str(candidate["title"]).strip()
            body = str(candidate["body"]).strip()
            if not title or not body:
                raise ValueError("语义候选标题和内容不能为空")
            if len(title) > 500 or len(body) > 100_000:
                raise ValueError("语义候选内容过长")
            evidence_json = json.dumps(
                candidate.get("evidence", {}),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            canonical_payload = {
                "candidate_key": str(candidate["candidate_key"]),
                "candidate_type": candidate_type,
                "session_start_ms": start_ms,
                "session_end_ms": end_ms,
                "title": title,
                "body": body,
                "confidence": confidence,
                "evidence": json.loads(evidence_json),
            }
            canonical = json.dumps(
                canonical_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            content_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            prepared.append(
                (
                    run_id,
                    str(candidate["candidate_key"]),
                    candidate_type,
                    start_ms,
                    end_ms,
                    title,
                    body,
                    float(confidence) if confidence is not None else None,
                    evidence_json,
                    content_sha256,
                    utc_now(),
                )
            )

        with self.connect() as connection:
            now = utc_now()
            connection.execute(
                """
                INSERT INTO semantic_exchanges (
                    run_id, session_id, asr_run_id, diarization_run_id,
                    provider, model, request_format, response_format,
                    request_json, request_sha256, response_json,
                    response_sha256, audio_bytes_included,
                    source_paths_included, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)
                """,
                (
                    run_id,
                    session_id,
                    asr_run_id,
                    int(diarization_run_id)
                    if diarization_run_id is not None
                    else None,
                    str(exchange["provider"]),
                    str(exchange["model"]),
                    str(exchange["request_format"]),
                    str(exchange["response_format"]),
                    request_json,
                    request_sha256,
                    response_json,
                    response_sha256,
                    now,
                ),
            )
            connection.executemany(
                """
                INSERT INTO semantic_candidates (
                    run_id, candidate_key, candidate_type,
                    session_start_ms, session_end_ms, title, body,
                    confidence, evidence_json, content_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                prepared,
            )
        return self.list_semantic_candidates(run_id)

    def get_semantic_exchange(self, run_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM semantic_exchanges WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"V2-E.0 run {run_id} 没有语义交换")
        return row

    def list_semantic_candidates(self, run_id: int) -> list[sqlite3.Row]:
        run = self.get_processing_run(run_id)
        if str(run["run_kind"]) != "semantic_v2e0":
            raise ValueError("语义候选只适用于 V2-E.0 run")
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM semantic_candidates
                    WHERE run_id = ?
                    ORDER BY CASE candidate_type
                        WHEN 'daily_summary' THEN 0
                        WHEN 'event' THEN 1
                        WHEN 'fact' THEN 2
                        ELSE 3 END,
                        session_start_ms, id
                    """,
                    (run_id,),
                )
            )

    def get_semantic_candidate(self, candidate_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM semantic_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"语义候选 {candidate_id} 不存在")
        return row

    def create_semantic_candidate_revision(
        self,
        candidate_id: int,
        *,
        status: str,
        title: str | None = None,
        body: str | None = None,
        note: str | None = None,
    ) -> sqlite3.Row:
        if status not in {"confirmed", "rejected"}:
            raise ValueError("语义审核状态必须是 confirmed 或 rejected")
        candidate = self.get_semantic_candidate(candidate_id)
        run = self.get_processing_run(int(candidate["run_id"]))
        if str(run["status"]) != "completed":
            raise ValueError("只能审核已完成 V2-E.0 run 的候选")
        with self.connect() as connection:
            latest = connection.execute(
                """
                SELECT * FROM semantic_candidate_revisions
                WHERE candidate_id = ? ORDER BY revision_index DESC LIMIT 1
                """,
                (candidate_id,),
            ).fetchone()
            current_title = str(latest["title"] if latest else candidate["title"])
            current_body = str(latest["body"] if latest else candidate["body"])
            revised_title = str(title).strip() if title is not None else current_title
            revised_body = str(body).strip() if body is not None else current_body
            if not revised_title or not revised_body:
                raise ValueError("语义审核后的标题和内容不能为空")
            if len(revised_title) > 500 or len(revised_body) > 100_000:
                raise ValueError("语义审核内容过长")
            revision_index = int(latest["revision_index"] if latest else 0) + 1
            payload = {
                "candidate_id": candidate_id,
                "revision_index": revision_index,
                "status": status,
                "title": revised_title,
                "body": revised_body,
                "note": note.strip() if note and note.strip() else None,
            }
            canonical = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            cursor = connection.execute(
                """
                INSERT INTO semantic_candidate_revisions (
                    candidate_id, revision_index, status, title, body,
                    note, content_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    revision_index,
                    status,
                    revised_title,
                    revised_body,
                    payload["note"],
                    hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                    utc_now(),
                ),
            )
            revision_id = int(cursor.lastrowid)
            return connection.execute(
                "SELECT * FROM semantic_candidate_revisions WHERE id = ?",
                (revision_id,),
            ).fetchone()

    def list_semantic_candidate_revisions(
        self, candidate_id: int
    ) -> list[sqlite3.Row]:
        self.get_semantic_candidate(candidate_id)
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM semantic_candidate_revisions
                    WHERE candidate_id = ? ORDER BY revision_index
                    """,
                    (candidate_id,),
                )
            )

    def upsert_identity_candidate_review(
        self, run_id: int, values: dict[str, Any]
    ) -> sqlite3.Row:
        run = self.get_processing_run(run_id)
        if str(run["run_kind"]) != "quality_diarization_v2d3":
            raise ValueError("身份候选审核只适用于 V2-D.3 run")
        if str(run["status"]) != "completed":
            raise ValueError("只能审核已完成的 V2-D.3 run")
        status = str(values["status"])
        if status not in {"confirmed_target", "rejected", "uncertain"}:
            raise ValueError("身份候选审核状态无效")
        start_ms = int(values["session_start_ms"])
        end_ms = int(values["session_end_ms"])
        if start_ms < 0 or end_ms <= start_ms:
            raise ValueError("身份候选审核时间范围无效")
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO identity_candidate_reviews (
                    run_id, candidate_id, target_identity, session_start_ms,
                    session_end_ms, status, note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, candidate_id) DO UPDATE SET
                    status = excluded.status,
                    note = excluded.note,
                    updated_at = excluded.updated_at
                """,
                (
                    run_id,
                    str(values["candidate_id"]),
                    str(values["target_identity"]),
                    start_ms,
                    end_ms,
                    status,
                    values.get("note"),
                    now,
                    now,
                ),
            )
            return connection.execute(
                """
                SELECT * FROM identity_candidate_reviews
                WHERE run_id = ? AND candidate_id = ?
                """,
                (run_id, str(values["candidate_id"])),
            ).fetchone()

    def list_identity_candidate_reviews(self, run_id: int) -> list[sqlite3.Row]:
        run = self.get_processing_run(run_id)
        if str(run["run_kind"]) != "quality_diarization_v2d3":
            raise ValueError("身份候选审核只适用于 V2-D.3 run")
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM identity_candidate_reviews
                    WHERE run_id = ? ORDER BY session_start_ms, id
                    """,
                    (run_id,),
                )
            )

    def upsert_identity_reference_interval(
        self, values: dict[str, Any]
    ) -> sqlite3.Row:
        """Save one source-backed identity reference without copying source audio."""
        identity_label = str(values["identity_label"]).strip()
        if not identity_label:
            raise ValueError("人物参考身份不能为空")
        decision = str(values["decision"])
        if decision not in {"confirmed_target", "rejected", "uncertain"}:
            raise ValueError("人物参考结论无效")
        provenance_kind = str(values["provenance_kind"])
        if provenance_kind not in {"truth", "v2d3_review"}:
            raise ValueError("人物参考来源无效")
        session_id = int(values["session_id"])
        source_object_id = int(values["source_object_id"])
        session_start = int(values["session_start_ms"])
        session_end = int(values["session_end_ms"])
        source_start = int(values["source_start_ms"])
        source_end = int(values["source_end_ms"])
        source_instance_id = values.get("source_instance_id")
        if session_start < 0 or session_end <= session_start:
            raise ValueError("人物参考会话时间范围无效")
        if source_start < 0 or source_end <= source_start:
            raise ValueError("人物参考原音时间范围无效")
        now = utc_now()
        with self.connect() as connection:
            source_matches = list(connection.execute(
                """
                SELECT so.sha256, ss.source_instance_id,
                       ss.session_start_ms, ss.session_end_ms,
                       ss.source_start_ms, ss.source_end_ms
                FROM source_objects so
                JOIN session_sources ss ON ss.source_object_id = so.id
                WHERE so.id = ? AND ss.session_id = ?
                  AND (? IS NULL OR ss.source_instance_id = ?)
                """,
                (source_object_id, session_id, source_instance_id, source_instance_id),
            ))
            if not source_matches:
                raise ValueError("人物参考原音不属于指定录音会话")
            if len(source_matches) > 1:
                raise ValueError("人物参考原音实例不明确")
            source = source_matches[0]
            source_instance_id = int(source["source_instance_id"])
            if str(source["sha256"]) != str(values["source_sha256"]):
                raise ValueError("人物参考原音 SHA-256 不一致")
            if (
                source_start < int(source["source_start_ms"])
                or source_end > int(source["source_end_ms"])
            ):
                raise ValueError("人物参考范围超出不可变原音映射")
            mapped_start = int(source["session_start_ms"]) + (
                source_start - int(source["source_start_ms"])
            )
            mapped_end = mapped_start + (source_end - source_start)
            if (mapped_start, mapped_end) != (session_start, session_end):
                raise ValueError("人物参考的会话时间与原音时间不对应")
            connection.execute(
                """
                INSERT INTO identity_reference_intervals (
                    reference_key, identity_label, decision, session_id,
                    session_start_ms, session_end_ms, source_object_id, source_instance_id,
                    source_sha256, source_start_ms, source_end_ms,
                    provenance_kind, provenance_id, metadata_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    identity_label, session_id, source_instance_id,
                    source_start_ms, source_end_ms
                ) DO UPDATE SET
                    decision = excluded.decision,
                    provenance_kind = excluded.provenance_kind,
                    provenance_id = excluded.provenance_id,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    str(values["reference_key"]),
                    identity_label,
                    decision,
                    session_id,
                    session_start,
                    session_end,
                    source_object_id,
                    source_instance_id,
                    str(values["source_sha256"]),
                    source_start,
                    source_end,
                    provenance_kind,
                    int(values["provenance_id"]),
                    json.dumps(
                        values.get("metadata", {}),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                    now,
                ),
            )
            return connection.execute(
                """
                SELECT * FROM identity_reference_intervals
                WHERE identity_label = ? AND session_id = ?
                  AND source_object_id = ? AND source_start_ms = ?
                  AND source_end_ms = ?
                """,
                (
                    identity_label,
                    session_id,
                    source_object_id,
                    source_start,
                    source_end,
                ),
            ).fetchone()

    def list_identity_reference_intervals(
        self, identity_label: str | None = None
    ) -> list[sqlite3.Row]:
        with self.connect() as connection:
            if identity_label is None:
                return list(
                    connection.execute(
                        """
                        SELECT * FROM identity_reference_intervals
                        ORDER BY identity_label, session_id, session_start_ms, id
                        """
                    )
                )
            return list(
                connection.execute(
                    """
                    SELECT * FROM identity_reference_intervals
                    WHERE identity_label = ?
                    ORDER BY session_id, session_start_ms, id
                    """,
                    (identity_label,),
                )
            )

    def list_processing_run_inputs(self, run_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM processing_run_inputs
                    WHERE run_id = ? ORDER BY position
                    """,
                    (run_id,),
                )
            )

    def get_processing_run(self, run_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM processing_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"处理运行 {run_id} 不存在")
        return row

    def create_asr_hypothesis(
        self, values: dict[str, Any], tokens: Sequence[dict[str, Any]]
    ) -> sqlite3.Row:
        """Append one immutable model hypothesis and its source-traced tokens."""
        run_id = int(values["run_id"])
        run = self.get_processing_run(run_id)
        session_id = int(values["session_id"])
        if int(run["session_id"]) != session_id:
            raise ValueError("ASR hypothesis session does not match its processing run")
        if str(run["status"]) != "running":
            raise ValueError("ASR hypotheses can only be appended to a running processing run")

        analysis_start = int(values["analysis_start_ms"])
        analysis_end = int(values["analysis_end_ms"])
        core_start = int(values["core_start_ms"])
        core_end = int(values["core_end_ms"])
        if not (
            0 <= analysis_start <= core_start < core_end <= analysis_end
        ):
            raise ValueError("ASR hypothesis has an invalid window range")

        canonical_payload = {
            "hypothesis_key": values["hypothesis_key"],
            "run_id": run_id,
            "session_id": session_id,
            "window_index": int(values["window_index"]),
            "hypothesis_role": values["hypothesis_role"],
            "core_start_ms": core_start,
            "core_end_ms": core_end,
            "analysis_start_ms": analysis_start,
            "analysis_end_ms": analysis_end,
            "model_id": values["model_id"],
            "model_revision": values.get("model_revision"),
            "backend": values["backend"],
            "language": values.get("language"),
            "text": values.get("text", ""),
            "parameters": values.get("parameters", {}),
            "raw_response": values.get("raw_response", {}),
            "tokens": list(tokens),
        }
        canonical_json = json.dumps(
            canonical_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        content_sha256 = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO asr_hypotheses (
                    hypothesis_key, run_id, session_id, window_index,
                    hypothesis_role, core_start_ms, core_end_ms,
                    analysis_start_ms, analysis_end_ms, model_id,
                    model_revision, backend, language, text, parameters_json,
                    raw_response_json, content_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["hypothesis_key"],
                    run_id,
                    session_id,
                    values["window_index"],
                    values["hypothesis_role"],
                    core_start,
                    core_end,
                    analysis_start,
                    analysis_end,
                    values["model_id"],
                    values.get("model_revision"),
                    values["backend"],
                    values.get("language"),
                    values.get("text", ""),
                    json.dumps(values.get("parameters", {}), ensure_ascii=False, sort_keys=True),
                    json.dumps(values.get("raw_response", {}), ensure_ascii=False, sort_keys=True),
                    content_sha256,
                    now,
                ),
            )
            hypothesis_id = int(cursor.lastrowid)
            for token_index, token in enumerate(tokens):
                token_start = int(token["session_start_ms"])
                token_end = int(token["session_end_ms"])
                if token_start < analysis_start or token_end > analysis_end or token_end <= token_start:
                    raise ValueError(f"ASR token {token_index} falls outside its analysis window")
                token_cursor = connection.execute(
                    """
                    INSERT INTO asr_alignment_tokens (
                        hypothesis_id, token_index, text, session_start_ms,
                        session_end_ms, analysis_start_ms, analysis_end_ms,
                        kept_in_core, confidence, alignment_model_id,
                        metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        hypothesis_id,
                        int(token.get("token_index", token_index)),
                        token["text"],
                        token_start,
                        token_end,
                        int(token["analysis_start_ms"]),
                        int(token["analysis_end_ms"]),
                        1 if token.get("kept_in_core", False) else 0,
                        token.get("confidence"),
                        token.get("alignment_model_id"),
                        json.dumps(token.get("metadata", {}), ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )
                token_id = int(token_cursor.lastrowid)
                source_refs = list(token.get("source_refs", []))
                if not source_refs:
                    raise ValueError(f"ASR token {token_index} has no immutable source reference")
                mapped_ms = 0
                mapped_cursor = token_start
                for position, source_ref in enumerate(source_refs):
                    source_instance_id = source_ref.get("source_instance_id")
                    source_matches = list(connection.execute(
                        """
                        SELECT so.sha256, ss.source_instance_id,
                               ss.session_start_ms, ss.session_end_ms,
                               ss.source_start_ms, ss.source_end_ms
                        FROM source_objects so
                        JOIN session_sources ss ON ss.source_object_id = so.id
                        WHERE so.id = ? AND ss.session_id = ?
                          AND (? IS NULL OR ss.source_instance_id = ?)
                        """,
                        (
                            source_ref["source_object_id"],
                            session_id,
                            source_instance_id,
                            source_instance_id,
                        ),
                    ))
                    if not source_matches:
                        raise ValueError("ASR token references a source outside its session")
                    if len(source_matches) > 1:
                        raise ValueError("ASR token source instance is ambiguous")
                    source = source_matches[0]
                    source_instance_id = int(source["source_instance_id"])
                    if str(source["sha256"]) != str(source_ref["source_sha256"]):
                        raise ValueError("ASR token source SHA-256 mismatch")
                    source_start = int(source_ref["source_start_ms"])
                    source_end = int(source_ref["source_end_ms"])
                    if (
                        source_start < int(source["source_start_ms"])
                        or source_end > int(source["source_end_ms"])
                        or source_end <= source_start
                    ):
                        raise ValueError("ASR token has an invalid source range")
                    mapped_start = int(source["session_start_ms"]) + (
                        source_start - int(source["source_start_ms"])
                    )
                    mapped_end = mapped_start + (source_end - source_start)
                    if mapped_start != mapped_cursor or mapped_end > token_end:
                        raise ValueError("ASR token source references are not continuous")
                    mapped_cursor = mapped_end
                    mapped_ms += source_end - source_start
                    connection.execute(
                        """
                        INSERT INTO asr_token_sources (
                            token_id, position, source_object_id,
                            source_instance_id, source_sha256,
                            source_start_ms, source_end_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            token_id,
                            position,
                            source_ref["source_object_id"],
                            source_instance_id,
                            source_ref["source_sha256"],
                            source_start,
                            source_end,
                        ),
                    )
                if mapped_ms != token_end - token_start or mapped_cursor != token_end:
                    raise ValueError(f"ASR token {token_index} source mapping is incomplete")
        return self.get_asr_hypothesis(hypothesis_id)

    def get_asr_hypothesis(self, hypothesis_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM asr_hypotheses WHERE id = ?", (hypothesis_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"ASR hypothesis {hypothesis_id} does not exist")
        return row

    def list_asr_hypotheses(
        self, run_id: int, *, role: str | None = None
    ) -> list[sqlite3.Row]:
        self.get_processing_run(run_id)
        sql = "SELECT * FROM asr_hypotheses WHERE run_id = ?"
        params: list[Any] = [run_id]
        if role is not None:
            sql += " AND hypothesis_role = ?"
            params.append(role)
        sql += " ORDER BY window_index, hypothesis_role"
        with self.connect() as connection:
            return list(connection.execute(sql, params))

    def list_asr_tokens(
        self, hypothesis_id: int, *, core_only: bool = False
    ) -> list[sqlite3.Row]:
        self.get_asr_hypothesis(hypothesis_id)
        sql = "SELECT * FROM asr_alignment_tokens WHERE hypothesis_id = ?"
        params: list[Any] = [hypothesis_id]
        if core_only:
            sql += " AND kept_in_core = 1"
        sql += " ORDER BY token_index"
        with self.connect() as connection:
            return list(connection.execute(sql, params))

    def list_asr_token_sources(self, token_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM asr_token_sources WHERE token_id = ? ORDER BY position",
                    (token_id,),
                )
            )

    def list_asr_token_sources_for_run(self, run_id: int) -> list[sqlite3.Row]:
        """Return source coordinates for every committed primary token in a run."""
        self.get_processing_run(run_id)
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT s.*, t.session_start_ms, t.session_end_ms
                    FROM asr_token_sources s
                    JOIN asr_alignment_tokens t ON t.id = s.token_id
                    JOIN asr_hypotheses h ON h.id = t.hypothesis_id
                    WHERE h.run_id = ? AND h.hypothesis_role = 'primary'
                      AND t.kept_in_core = 1
                    ORDER BY t.session_start_ms, t.session_end_ms,
                             t.id, s.position
                    """,
                    (run_id,),
                )
            )

    def list_committed_asr_tokens(self, run_id: int) -> list[sqlite3.Row]:
        """Return immutable primary, core-only tokens for one completed ASR run."""
        self.get_processing_run(run_id)
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT t.*, h.run_id AS asr_run_id, h.window_index,
                           h.model_id, h.hypothesis_key
                    FROM asr_alignment_tokens t
                    JOIN asr_hypotheses h ON h.id = t.hypothesis_id
                    WHERE h.run_id = ? AND h.hypothesis_role = 'primary'
                      AND t.kept_in_core = 1
                    ORDER BY t.session_start_ms, t.session_end_ms, t.id
                    """,
                    (run_id,),
                )
            )

    def create_diarization_turns(
        self, run_id: int, session_id: int, turns: Sequence[dict[str, Any]]
    ) -> list[sqlite3.Row]:
        """Append overlap-aware, source-traced speaker turns to a running V2-D run."""
        run = self.get_processing_run(run_id)
        session = self.get_recording_session(session_id)
        if str(run["run_kind"]) != "quality_diarization_v2d":
            raise ValueError("speaker turns require a V2-D diarization run")
        if str(run["status"]) != "running":
            raise ValueError("speaker turns can only be appended to a running run")
        if int(run["session_id"]) != session_id:
            raise ValueError("speaker turn session does not match its processing run")
        duration_ms = int(session["duration_ms"])
        now = utc_now()
        created_ids: list[int] = []
        with self.connect() as connection:
            for turn in turns:
                start_ms = int(turn["session_start_ms"])
                end_ms = int(turn["session_end_ms"])
                if start_ms < 0 or end_ms <= start_ms or end_ms > duration_ms:
                    raise ValueError("speaker turn falls outside its recording session")
                source_refs = list(turn.get("source_refs", []))
                if not source_refs:
                    raise ValueError("speaker turn has no immutable source reference")
                canonical_payload = {
                    "turn_key": str(turn["turn_key"]),
                    "run_id": run_id,
                    "session_id": session_id,
                    "turn_index": int(turn["turn_index"]),
                    "turn_kind": str(turn["turn_kind"]),
                    "speaker_label": str(turn["speaker_label"]),
                    "session_start_ms": start_ms,
                    "session_end_ms": end_ms,
                    "confidence": turn.get("confidence"),
                    "metadata": turn.get("metadata", {}),
                    "source_refs": source_refs,
                }
                canonical = json.dumps(
                    canonical_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                content_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                cursor = connection.execute(
                    """
                    INSERT INTO diarization_turns (
                        turn_key, run_id, session_id, turn_index, turn_kind,
                        speaker_label, session_start_ms, session_end_ms,
                        confidence, metadata_json, content_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        turn["turn_key"],
                        run_id,
                        session_id,
                        turn["turn_index"],
                        turn["turn_kind"],
                        turn["speaker_label"],
                        start_ms,
                        end_ms,
                        turn.get("confidence"),
                        json.dumps(
                            turn.get("metadata", {}),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        content_sha256,
                        now,
                    ),
                )
                turn_id = int(cursor.lastrowid)
                created_ids.append(turn_id)
                mapped_cursor = start_ms
                mapped_ms = 0
                for position, source_ref in enumerate(source_refs):
                    source_instance_id = source_ref.get("source_instance_id")
                    source_matches = list(connection.execute(
                        """
                        SELECT so.sha256, ss.source_instance_id,
                               ss.session_start_ms, ss.session_end_ms,
                               ss.source_start_ms, ss.source_end_ms
                        FROM source_objects so
                        JOIN session_sources ss ON ss.source_object_id = so.id
                        WHERE so.id = ? AND ss.session_id = ?
                          AND (? IS NULL OR ss.source_instance_id = ?)
                        """,
                        (
                            source_ref["source_object_id"],
                            session_id,
                            source_instance_id,
                            source_instance_id,
                        ),
                    ))
                    if not source_matches:
                        raise ValueError("speaker turn references a source outside its session")
                    if len(source_matches) > 1:
                        raise ValueError("speaker turn source instance is ambiguous")
                    source = source_matches[0]
                    source_instance_id = int(source["source_instance_id"])
                    if str(source["sha256"]) != str(source_ref["source_sha256"]):
                        raise ValueError("speaker turn source SHA-256 mismatch")
                    source_start = int(source_ref["source_start_ms"])
                    source_end = int(source_ref["source_end_ms"])
                    if (
                        source_start < int(source["source_start_ms"])
                        or source_end > int(source["source_end_ms"])
                        or source_end <= source_start
                    ):
                        raise ValueError("speaker turn has an invalid source range")
                    mapped_start = int(source["session_start_ms"]) + (
                        source_start - int(source["source_start_ms"])
                    )
                    mapped_end = mapped_start + (source_end - source_start)
                    if mapped_start != mapped_cursor or mapped_end > end_ms:
                        raise ValueError("speaker turn source references are not continuous")
                    mapped_cursor = mapped_end
                    mapped_ms += source_end - source_start
                    connection.execute(
                        """
                        INSERT INTO diarization_turn_sources (
                            turn_id, position, source_object_id,
                            source_instance_id, source_sha256,
                            source_start_ms, source_end_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            turn_id,
                            position,
                            source_ref["source_object_id"],
                            source_instance_id,
                            source_ref["source_sha256"],
                            source_start,
                            source_end,
                        ),
                    )
                if mapped_ms != end_ms - start_ms or mapped_cursor != end_ms:
                    raise ValueError("speaker turn source mapping is incomplete")
        return self.list_diarization_turns(run_id) if created_ids else []

    def get_diarization_turn(self, turn_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM diarization_turns WHERE id = ?", (turn_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"说话人时间段 {turn_id} 不存在")
        return row

    def list_diarization_turns(
        self, run_id: int, *, turn_kind: str | None = None
    ) -> list[sqlite3.Row]:
        self.get_processing_run(run_id)
        sql = "SELECT * FROM diarization_turns WHERE run_id = ?"
        params: list[Any] = [run_id]
        if turn_kind is not None:
            sql += " AND turn_kind = ?"
            params.append(turn_kind)
        sql += " ORDER BY session_start_ms, session_end_ms, speaker_label, id"
        with self.connect() as connection:
            return list(connection.execute(sql, params))

    def list_diarization_turn_sources(self, turn_id: int) -> list[sqlite3.Row]:
        self.get_diarization_turn(turn_id)
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM diarization_turn_sources
                    WHERE turn_id = ? ORDER BY position
                    """,
                    (turn_id,),
                )
            )

    def create_token_speaker_attributions(
        self,
        run_id: int,
        asr_run_id: int,
        attributions: Sequence[dict[str, Any]],
    ) -> list[sqlite3.Row]:
        """Append immutable token-to-speaker decisions, including overlap and none."""
        run = self.get_processing_run(run_id)
        asr_run = self.get_processing_run(asr_run_id)
        if str(run["run_kind"]) != "quality_diarization_v2d":
            raise ValueError("speaker attribution requires a V2-D diarization run")
        if str(run["status"]) != "running":
            raise ValueError("speaker attribution can only be appended to a running run")
        if str(asr_run["run_kind"]) != "quality_asr_v2c":
            raise ValueError("speaker attribution requires a V2-C ASR run")
        if str(asr_run["status"]) != "completed":
            raise ValueError("speaker attribution requires a completed V2-C ASR run")
        if int(run["session_id"]) != int(asr_run["session_id"]):
            raise ValueError("diarization and ASR runs belong to different sessions")
        now = utc_now()
        created_ids: list[int] = []
        with self.connect() as connection:
            for value in attributions:
                token_id = int(value["token_id"])
                token = connection.execute(
                    """
                    SELECT t.id, t.kept_in_core, h.run_id, h.hypothesis_role
                    FROM asr_alignment_tokens t
                    JOIN asr_hypotheses h ON h.id = t.hypothesis_id
                    WHERE t.id = ?
                    """,
                    (token_id,),
                ).fetchone()
                if (
                    token is None
                    or int(token["run_id"]) != asr_run_id
                    or str(token["hypothesis_role"]) != "primary"
                    or not bool(token["kept_in_core"])
                ):
                    raise ValueError("attribution token is not a committed primary ASR token")
                canonical_payload = {
                    "attribution_key": str(value["attribution_key"]),
                    "run_id": run_id,
                    "asr_run_id": asr_run_id,
                    "token_id": token_id,
                    "speaker_label": value.get("speaker_label"),
                    "attribution_kind": str(value["attribution_kind"]),
                    "overlap_ms": int(value.get("overlap_ms", 0)),
                    "overlap_ratio": float(value.get("overlap_ratio", 0.0)),
                    "rank": int(value["rank"]),
                    "confidence": value.get("confidence"),
                    "metadata": value.get("metadata", {}),
                }
                canonical = json.dumps(
                    canonical_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                content_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                cursor = connection.execute(
                    """
                    INSERT INTO token_speaker_attributions (
                        attribution_key, run_id, asr_run_id, token_id,
                        speaker_label, attribution_kind, overlap_ms,
                        overlap_ratio, rank, confidence, metadata_json,
                        content_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        value["attribution_key"],
                        run_id,
                        asr_run_id,
                        token_id,
                        value.get("speaker_label"),
                        value["attribution_kind"],
                        int(value.get("overlap_ms", 0)),
                        float(value.get("overlap_ratio", 0.0)),
                        int(value["rank"]),
                        value.get("confidence"),
                        json.dumps(
                            value.get("metadata", {}),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        content_sha256,
                        now,
                    ),
                )
                created_ids.append(int(cursor.lastrowid))
        return self.list_token_speaker_attributions(run_id) if created_ids else []

    def list_token_speaker_attributions(
        self, run_id: int, *, token_id: int | None = None
    ) -> list[sqlite3.Row]:
        self.get_processing_run(run_id)
        sql = "SELECT * FROM token_speaker_attributions WHERE run_id = ?"
        params: list[Any] = [run_id]
        if token_id is not None:
            sql += " AND token_id = ?"
            params.append(token_id)
        sql += " ORDER BY token_id, rank"
        with self.connect() as connection:
            return list(connection.execute(sql, params))

    def create_asr_disagreement(self, values: dict[str, Any]) -> sqlite3.Row:
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO asr_disagreements (
                    run_id, window_index, primary_hypothesis_id,
                    secondary_hypothesis_id, session_start_ms, session_end_ms,
                    normalized_distance, priority, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["run_id"],
                    values["window_index"],
                    values["primary_hypothesis_id"],
                    values["secondary_hypothesis_id"],
                    values["session_start_ms"],
                    values["session_end_ms"],
                    values["normalized_distance"],
                    values["priority"],
                    json.dumps(values.get("details", {}), ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            disagreement_id = int(cursor.lastrowid)
            return connection.execute(
                "SELECT * FROM asr_disagreements WHERE id = ?", (disagreement_id,)
            ).fetchone()

    def list_asr_disagreements(self, run_id: int) -> list[sqlite3.Row]:
        self.get_processing_run(run_id)
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM asr_disagreements
                    WHERE run_id = ?
                    ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                             window_index
                    """,
                    (run_id,),
                )
            )

    def segment_count(self, recording_id: int) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM speech_segments WHERE recording_id = ?",
                (recording_id,),
            ).fetchone()
            return int(row["count"])

    def replace_vad_segments(
        self,
        recording_id: int,
        segments: Sequence[tuple[int, int]],
        source_path: str,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM speech_segments WHERE recording_id = ?", (recording_id,)
            )
            connection.executemany(
                """
                INSERT INTO speech_segments (
                    recording_id, segment_index, start_ms, end_ms, audio_ref,
                    asr_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                [
                    (
                        recording_id,
                        index,
                        start_ms,
                        end_ms,
                        f"{source_path}#t={start_ms / 1000:.3f},{end_ms / 1000:.3f}",
                        now,
                        now,
                    )
                    for index, (start_ms, end_ms) in enumerate(segments)
                ],
            )

    def reset_failed_segments(self, recording_id: int) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE speech_segments
                SET asr_status = 'pending', error = NULL, updated_at = ?
                WHERE recording_id = ? AND asr_status = 'failed'
                """,
                (utc_now(), recording_id),
            )
            return cursor.rowcount

    def reset_interrupted_segments(self, recording_id: int) -> int:
        """Recover segments left in running state by an interrupted process."""
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE speech_segments
                SET asr_status = 'pending', error = '上次处理被中断，已自动恢复', updated_at = ?
                WHERE recording_id = ? AND asr_status = 'running'
                """,
                (utc_now(), recording_id),
            )
            return cursor.rowcount

    def reset_all_asr_segments(self, recording_id: int) -> int:
        """Explicitly discard derived ASR text while preserving VAD boundaries."""
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE speech_segments
                SET asr_status = 'pending', language = NULL, text_raw = NULL,
                    text_display = NULL, asr_model = NULL, error = NULL, updated_at = ?
                WHERE recording_id = ?
                """,
                (utc_now(), recording_id),
            )
            return cursor.rowcount

    def pending_segments(
        self, recording_id: int, limit: int | None = None
    ) -> list[sqlite3.Row]:
        sql = """
            SELECT * FROM speech_segments
            WHERE recording_id = ? AND asr_status = 'pending'
            ORDER BY segment_index
        """
        params: list[Any] = [recording_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as connection:
            return list(connection.execute(sql, params))

    def all_segments(self, recording_id: int, completed_only: bool = False) -> list[sqlite3.Row]:
        sql = """
            SELECT speech_segments.*, person_profiles.display_name AS person_name
            FROM speech_segments
            LEFT JOIN person_profiles ON person_profiles.id = speech_segments.person_id
            WHERE speech_segments.recording_id = ?
        """
        if completed_only:
            sql += " AND speech_segments.asr_status = 'completed'"
        sql += " ORDER BY speech_segments.segment_index"
        with self.connect() as connection:
            return list(connection.execute(sql, (recording_id,)))

    def mark_segment_running(self, segment_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE speech_segments
                SET asr_status = 'running', attempts = attempts + 1, error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (utc_now(), segment_id),
            )

    def mark_segment_completed(
        self,
        segment_id: int,
        *,
        language: str | None,
        text_raw: str,
        text_display: str,
        asr_model: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE speech_segments
                SET asr_status = 'completed', language = ?, text_raw = ?, text_display = ?,
                    asr_model = ?, error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (language, text_raw, text_display, asr_model, utc_now(), segment_id),
            )

    def mark_segment_failed(self, segment_id: int, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE speech_segments
                SET asr_status = 'failed', error = ?, updated_at = ?
                WHERE id = ?
                """,
                (error[:2000], utc_now(), segment_id),
            )

    def segment_status_counts(self, recording_id: int) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT asr_status, COUNT(*) AS count
                FROM speech_segments WHERE recording_id = ? GROUP BY asr_status
                """,
                (recording_id,),
            )
            return {row["asr_status"]: int(row["count"]) for row in rows}

    def replace_speaker_labels(
        self, recording_id: int, labels: Sequence[tuple[int, str | None]]
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE speech_segments
                SET speaker_session_id = NULL, person_id = NULL,
                    speaker_match_score = NULL, updated_at = ?
                WHERE recording_id = ?
                """,
                (now, recording_id),
            )
            connection.executemany(
                """
                UPDATE speech_segments
                SET speaker_session_id = ?, updated_at = ?
                WHERE id = ? AND recording_id = ?
                """,
                [(label, now, segment_id, recording_id) for segment_id, label in labels],
            )

    def get_segment(self, segment_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM speech_segments WHERE id = ?", (segment_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"片段 {segment_id} 不存在")
        return row

    def upsert_self_profile(
        self,
        *,
        display_name: str,
        embedding_model: str,
        embedding_version: str,
        embedding_path: str,
    ) -> sqlite3.Row:
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM person_profiles WHERE profile_type = 'self' ORDER BY id LIMIT 1"
            ).fetchone()
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO person_profiles (
                        display_name, profile_type, embedding_model,
                        embedding_version, embedding_path, created_at
                    ) VALUES (?, 'self', ?, ?, ?, ?)
                    """,
                    (display_name, embedding_model, embedding_version, embedding_path, utc_now()),
                )
                profile_id = int(cursor.lastrowid)
            else:
                profile_id = int(existing["id"])
                connection.execute(
                    """
                    UPDATE person_profiles
                    SET display_name = ?, embedding_model = ?, embedding_version = ?, embedding_path = ?
                    WHERE id = ?
                    """,
                    (display_name, embedding_model, embedding_version, embedding_path, profile_id),
                )
            return connection.execute(
                "SELECT * FROM person_profiles WHERE id = ?", (profile_id,)
            ).fetchone()

    def get_self_profile(self) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM person_profiles WHERE profile_type = 'self' ORDER BY id LIMIT 1"
            ).fetchone()

    def upsert_known_person_profile(
        self,
        *,
        display_name: str,
        embedding_model: str,
        embedding_version: str,
        embedding_path: str,
    ) -> sqlite3.Row:
        with self.connect() as connection:
            existing = connection.execute(
                """
                SELECT * FROM person_profiles
                WHERE profile_type = 'known_person' AND display_name = ?
                ORDER BY id LIMIT 1
                """,
                (display_name,),
            ).fetchone()
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO person_profiles (
                        display_name, profile_type, embedding_model,
                        embedding_version, embedding_path, created_at
                    ) VALUES (?, 'known_person', ?, ?, ?, ?)
                    """,
                    (
                        display_name,
                        embedding_model,
                        embedding_version,
                        embedding_path,
                        utc_now(),
                    ),
                )
                profile_id = int(cursor.lastrowid)
            else:
                profile_id = int(existing["id"])
                connection.execute(
                    """
                    UPDATE person_profiles
                    SET embedding_model = ?, embedding_version = ?, embedding_path = ?
                    WHERE id = ?
                    """,
                    (embedding_model, embedding_version, embedding_path, profile_id),
                )
            return connection.execute(
                "SELECT * FROM person_profiles WHERE id = ?", (profile_id,)
            ).fetchone()

    def get_person_profile(self, person_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM person_profiles WHERE id = ?", (person_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"人物 {person_id} 不存在")
        return row

    def list_person_profiles(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(connection.execute("SELECT * FROM person_profiles ORDER BY id"))

    def clear_person_assignments(self, recording_id: int, person_id: int) -> int:
        """Remove a person's derived identity from one recording, preserving speaker labels."""
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE speech_segments
                SET person_id = NULL, speaker_match_score = NULL, updated_at = ?
                WHERE recording_id = ? AND person_id = ?
                """,
                (utc_now(), recording_id, person_id),
            )
            return cursor.rowcount

    def person_assignment_count(self, person_id: int) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM speech_segments WHERE person_id = ?",
                (person_id,),
            ).fetchone()
            return int(row["count"])

    def delete_person_profile(self, person_id: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM person_profiles WHERE id = ?", (person_id,)
            )
            return cursor.rowcount == 1

    def assign_person_to_speaker(
        self,
        recording_id: int,
        speaker_session_id: str,
        person_id: int,
        score: float = 1.0,
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE speech_segments
                SET person_id = ?, speaker_match_score = ?, updated_at = ?
                WHERE recording_id = ? AND speaker_session_id = ?
                """,
                (person_id, score, utc_now(), recording_id, speaker_session_id),
            )
            return cursor.rowcount

    def assign_person_to_segments(
        self,
        recording_id: int,
        person_id: int,
        segment_scores: Sequence[tuple[int, float]],
    ) -> int:
        if not segment_scores:
            return 0
        with self.connect() as connection:
            before = connection.total_changes
            connection.executemany(
                """
                UPDATE speech_segments
                SET person_id = ?, speaker_match_score = ?, updated_at = ?
                WHERE recording_id = ? AND id = ?
                """,
                [
                    (person_id, score, utc_now(), recording_id, segment_id)
                    for segment_id, score in segment_scores
                ],
            )
            return connection.total_changes - before

    def upsert_segment_annotations(
        self, recording_id: int, annotations: Sequence[dict[str, Any]]
    ) -> int:
        if not annotations:
            return 0
        segment_ids = [int(item["segment_id"]) for item in annotations]
        placeholders = ",".join("?" for _ in segment_ids)
        now = utc_now()
        with self.connect() as connection:
            valid_rows = connection.execute(
                f"""
                SELECT id FROM speech_segments
                WHERE recording_id = ? AND id IN ({placeholders})
                """,
                (recording_id, *segment_ids),
            ).fetchall()
            if len(valid_rows) != len(set(segment_ids)):
                raise ValueError("标注中包含不属于当前录音的片段")
            connection.executemany(
                """
                INSERT INTO segment_identity_annotations (
                    recording_id, segment_id, identity_label, raw_label,
                    confidence, annotation_source, note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(recording_id, segment_id) DO UPDATE SET
                    identity_label = excluded.identity_label,
                    raw_label = excluded.raw_label,
                    confidence = excluded.confidence,
                    annotation_source = excluded.annotation_source,
                    note = excluded.note,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        recording_id,
                        int(item["segment_id"]),
                        item["identity_label"],
                        item["raw_label"],
                        item["confidence"],
                        item.get("annotation_source", "human"),
                        item.get("note"),
                        now,
                        now,
                    )
                    for item in annotations
                ],
            )
        return len(annotations)

    def list_segment_annotations(self, recording_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM segment_identity_annotations
                    WHERE recording_id = ? ORDER BY segment_id
                    """,
                    (recording_id,),
                )
            )

    def upsert_voice_library_sample(self, values: dict[str, Any]) -> sqlite3.Row:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO voice_library_samples (
                    sample_key, person_id, identity_label, sample_type, split,
                    source_path, stored_path, source_sha256, recording_id, segment_id,
                    session_key, duration_ms, speech_ms, embedding_blob, embedding_dim,
                    embedding_model, embedding_version, match_score, human_confirmed,
                    metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sample_key) DO UPDATE SET
                    person_id = excluded.person_id,
                    identity_label = excluded.identity_label,
                    sample_type = excluded.sample_type,
                    split = excluded.split,
                    source_path = excluded.source_path,
                    stored_path = excluded.stored_path,
                    source_sha256 = excluded.source_sha256,
                    recording_id = excluded.recording_id,
                    segment_id = excluded.segment_id,
                    session_key = excluded.session_key,
                    duration_ms = excluded.duration_ms,
                    speech_ms = excluded.speech_ms,
                    embedding_blob = excluded.embedding_blob,
                    embedding_dim = excluded.embedding_dim,
                    embedding_model = excluded.embedding_model,
                    embedding_version = excluded.embedding_version,
                    match_score = excluded.match_score,
                    human_confirmed = excluded.human_confirmed,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    values["sample_key"],
                    values.get("person_id"),
                    values["identity_label"],
                    values["sample_type"],
                    values["split"],
                    values["source_path"],
                    values.get("stored_path"),
                    values.get("source_sha256"),
                    values.get("recording_id"),
                    values.get("segment_id"),
                    values["session_key"],
                    values["duration_ms"],
                    values["speech_ms"],
                    values.get("embedding_blob"),
                    values.get("embedding_dim"),
                    values.get("embedding_model"),
                    values.get("embedding_version"),
                    values.get("match_score"),
                    1 if values.get("human_confirmed", True) else 0,
                    json.dumps(values.get("metadata", {}), ensure_ascii=False),
                    now,
                    now,
                ),
            )
            return connection.execute(
                "SELECT * FROM voice_library_samples WHERE sample_key = ?",
                (values["sample_key"],),
            ).fetchone()

    def list_voice_library_samples(
        self, *, person_id: int | None = None, recording_id: int | None = None
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if person_id is not None:
            clauses.append("person_id = ?")
            params.append(person_id)
        if recording_id is not None:
            clauses.append("recording_id = ?")
            params.append(recording_id)
        sql = "SELECT * FROM voice_library_samples"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self.connect() as connection:
            return list(connection.execute(sql, params))

    def replace_conversation_events(
        self,
        recording_id: int,
        events: Sequence[dict[str, Any]],
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM conversation_events WHERE recording_id = ?", (recording_id,)
            )
            connection.executemany(
                """
                INSERT INTO conversation_events (
                    recording_id, start_ms, end_ms, title, summary,
                    segment_ids_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        recording_id,
                        event["start_ms"],
                        event["end_ms"],
                        event.get("title"),
                        event.get("summary"),
                        json.dumps(event["segment_ids"], ensure_ascii=False),
                        now,
                    )
                    for event in events
                ],
            )

    def list_conversation_events(self, recording_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM conversation_events
                    WHERE recording_id = ? ORDER BY start_ms
                    """,
                    (recording_id,),
                )
            )

    def record_evaluation_run(
        self,
        recording_id: int,
        *,
        truth_path: str,
        truth_sha256: str,
        config: dict[str, Any],
        metrics: dict[str, Any],
        report_json_path: str,
        report_markdown_path: str,
    ) -> int:
        self.get_recording(recording_id)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO evaluation_runs (
                    recording_id, truth_path, truth_sha256, config_json,
                    metrics_json, report_json_path, report_markdown_path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recording_id,
                    truth_path,
                    truth_sha256,
                    json.dumps(config, ensure_ascii=False, sort_keys=True),
                    json.dumps(metrics, ensure_ascii=False, sort_keys=True),
                    report_json_path,
                    report_markdown_path,
                    utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    def list_evaluation_runs(self, recording_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM evaluation_runs
                    WHERE recording_id = ? ORDER BY id
                    """,
                    (recording_id,),
                )
            )

    def create_truth_set(
        self, values: dict[str, Any], annotations: Sequence[dict[str, Any]]
    ) -> sqlite3.Row:
        session_id = int(values["session_id"])
        session = self.get_recording_session(session_id)
        expected_fingerprint = self.session_input_fingerprint(session_id)
        if str(values["input_fingerprint"]) != expected_fingerprint:
            raise ValueError("真值集的输入指纹与录音会话不一致")
        scope_start = int(values["scope_start_ms"])
        scope_end = int(values["scope_end_ms"])
        if scope_start < 0 or scope_end <= scope_start:
            raise ValueError("真值集时间范围无效")
        if scope_end > int(session["duration_ms"]):
            raise ValueError("真值集时间范围超过录音会话")
        truth_path = Path(str(values["truth_path"])).resolve(strict=True)
        actual_truth_sha256 = _sha256_path(truth_path)
        if str(values["truth_sha256"]) != actual_truth_sha256:
            raise ValueError("真值文件声明的 SHA-256 不正确")
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO truth_sets (
                    truth_key, name, session_id, parent_truth_set_id,
                    format_version, status, scope_start_ms, scope_end_ms,
                    input_fingerprint, completeness_json, truth_path,
                    truth_sha256, provenance_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["truth_key"],
                    values["name"],
                    values["session_id"],
                    values.get("parent_truth_set_id"),
                    values["format_version"],
                    values["scope_start_ms"],
                    values["scope_end_ms"],
                    values["input_fingerprint"],
                    json.dumps(
                        values.get("completeness", {}),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    str(truth_path),
                    actual_truth_sha256,
                    json.dumps(
                        values.get("provenance", {}),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                    now,
                ),
            )
            truth_set_id = int(cursor.lastrowid)
            for annotation in annotations:
                annotation_start = int(annotation["session_start_ms"])
                annotation_end = int(annotation["session_end_ms"])
                if (
                    annotation_start < scope_start
                    or annotation_end > scope_end
                    or annotation_end <= annotation_start
                ):
                    raise ValueError(
                        f"真值标注 {annotation['annotation_key']} 超出真值集时间范围"
                    )
                source_refs = list(annotation.get("source_refs", []))
                if not source_refs:
                    raise ValueError(
                        f"真值标注 {annotation['annotation_key']} 没有原始音频引用"
                    )
                annotation_cursor = connection.execute(
                    """
                    INSERT INTO truth_annotations (
                        truth_set_id, annotation_key, annotation_kind,
                        session_start_ms, session_end_ms, label, text,
                        metadata_json, legacy_segment_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        truth_set_id,
                        annotation["annotation_key"],
                        annotation["annotation_kind"],
                        annotation["session_start_ms"],
                        annotation["session_end_ms"],
                        annotation.get("label"),
                        annotation.get("text"),
                        json.dumps(
                            annotation.get("metadata", {}),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        annotation.get("legacy_segment_id"),
                        now,
                    ),
                )
                annotation_id = int(annotation_cursor.lastrowid)
                mapped_cursor = annotation_start
                for position, source_ref in enumerate(source_refs):
                    source_instance_id = source_ref.get("source_instance_id")
                    source_matches = list(connection.execute(
                        """
                        SELECT so.sha256, ss.source_instance_id,
                               ss.session_start_ms, ss.session_end_ms,
                               ss.source_start_ms, ss.source_end_ms
                        FROM source_objects so
                        JOIN session_sources ss ON ss.source_object_id = so.id
                        WHERE so.id = ? AND ss.session_id = ?
                          AND (? IS NULL OR ss.source_instance_id = ?)
                        """,
                        (
                            source_ref["source_object_id"],
                            session_id,
                            source_instance_id,
                            source_instance_id,
                        ),
                    ))
                    if not source_matches:
                        raise KeyError(
                            f"原始音频对象 {source_ref['source_object_id']} 不属于当前会话"
                        )
                    if len(source_matches) > 1:
                        raise ValueError("真值源引用的原始音频实例不明确")
                    source = source_matches[0]
                    source_instance_id = int(source["source_instance_id"])
                    if str(source["sha256"]) != str(source_ref["source_sha256"]):
                        raise ValueError("真值源引用的 SHA-256 与不可变源对象不一致")
                    source_start = int(source_ref["source_start_ms"])
                    source_end = int(source_ref["source_end_ms"])
                    if (
                        source_start < int(source["source_start_ms"])
                        or source_end > int(source["source_end_ms"])
                        or source_end <= source_start
                    ):
                        raise ValueError("真值源引用超出原始对象在会话中的映射范围")
                    mapped_start = int(source["session_start_ms"]) + (
                        source_start - int(source["source_start_ms"])
                    )
                    mapped_end = mapped_start + (source_end - source_start)
                    if mapped_start != mapped_cursor or mapped_end > annotation_end:
                        raise ValueError("真值源引用没有连续覆盖标注时间范围")
                    mapped_cursor = mapped_end
                    connection.execute(
                        """
                        INSERT INTO truth_annotation_sources (
                            annotation_id, position, source_object_id, source_instance_id,
                            source_sha256, source_start_ms, source_end_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            annotation_id,
                            position,
                            source_ref["source_object_id"],
                            source_instance_id,
                            source_ref["source_sha256"],
                            source_start,
                            source_end,
                        ),
                    )
                if mapped_cursor != annotation_end:
                    raise ValueError("真值源引用没有完整覆盖标注时间范围")
            connection.execute(
                """
                UPDATE truth_sets
                SET status = 'frozen', frozen_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, truth_set_id),
            )
        return self.get_truth_set(truth_set_id)

    def get_truth_set(self, truth_set_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM truth_sets WHERE id = ?", (truth_set_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"连续时间真值集 {truth_set_id} 不存在")
        return row

    def list_truth_sets(self, session_id: int | None = None) -> list[sqlite3.Row]:
        with self.connect() as connection:
            if session_id is None:
                return list(connection.execute("SELECT * FROM truth_sets ORDER BY id"))
            return list(
                connection.execute(
                    "SELECT * FROM truth_sets WHERE session_id = ? ORDER BY id",
                    (session_id,),
                )
            )

    def list_truth_annotations(
        self, truth_set_id: int, *, annotation_kind: str | None = None
    ) -> list[sqlite3.Row]:
        self.get_truth_set(truth_set_id)
        sql = "SELECT * FROM truth_annotations WHERE truth_set_id = ?"
        params: list[Any] = [truth_set_id]
        if annotation_kind is not None:
            sql += " AND annotation_kind = ?"
            params.append(annotation_kind)
        sql += " ORDER BY session_start_ms, session_end_ms, id"
        with self.connect() as connection:
            return list(connection.execute(sql, params))

    def list_truth_annotation_sources(self, annotation_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM truth_annotation_sources
                    WHERE annotation_id = ? ORDER BY position
                    """,
                    (annotation_id,),
                )
            )

    def create_benchmark_prediction_set(
        self, values: dict[str, Any], predictions: Sequence[dict[str, Any]]
    ) -> sqlite3.Row:
        session_id = int(values["session_id"])
        session = self.get_recording_session(session_id)
        expected_fingerprint = self.session_input_fingerprint(session_id)
        if str(values["input_fingerprint"]) != expected_fingerprint:
            raise ValueError("预测快照的输入指纹与录音会话不一致")
        canonical = json.dumps(
            list(predictions),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        actual_content_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        supplied_content_sha256 = values.get("content_sha256")
        if (
            supplied_content_sha256 is not None
            and str(supplied_content_sha256) != actual_content_sha256
        ):
            raise ValueError("预测快照声明的内容 SHA-256 不正确")
        for prediction in predictions:
            start_ms = int(prediction["session_start_ms"])
            end_ms = int(prediction["session_end_ms"])
            if start_ms < 0 or end_ms <= start_ms or end_ms > int(session["duration_ms"]):
                raise ValueError(
                    f"预测 {prediction['prediction_key']} 超出录音会话时间范围"
                )
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO benchmark_prediction_sets (
                    prediction_key, name, session_id, processing_run_id,
                    input_fingerprint, adapter, model_manifest_json,
                    content_sha256, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?)
                """,
                (
                    values["prediction_key"],
                    values["name"],
                    values["session_id"],
                    values.get("processing_run_id"),
                    values["input_fingerprint"],
                    values["adapter"],
                    json.dumps(
                        values.get("model_manifest", {}),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    actual_content_sha256,
                    now,
                ),
            )
            prediction_set_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO benchmark_predictions (
                    prediction_set_id, prediction_key, prediction_kind,
                    session_start_ms, session_end_ms, label, text,
                    confidence, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        prediction_set_id,
                        prediction["prediction_key"],
                        prediction["prediction_kind"],
                        prediction["session_start_ms"],
                        prediction["session_end_ms"],
                        prediction.get("label"),
                        prediction.get("text"),
                        prediction.get("confidence"),
                        json.dumps(
                            prediction.get("metadata", {}),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        now,
                    )
                    for prediction in predictions
                ],
            )
            connection.execute(
                """
                UPDATE benchmark_prediction_sets
                SET status = 'frozen', frozen_at = ?
                WHERE id = ?
                """,
                (now, prediction_set_id),
            )
        return self.get_benchmark_prediction_set(prediction_set_id)

    def get_benchmark_prediction_set(self, prediction_set_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM benchmark_prediction_sets WHERE id = ?",
                (prediction_set_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"预测快照 {prediction_set_id} 不存在")
        return row

    def list_benchmark_prediction_sets(
        self, session_id: int | None = None
    ) -> list[sqlite3.Row]:
        with self.connect() as connection:
            if session_id is None:
                return list(
                    connection.execute(
                        "SELECT * FROM benchmark_prediction_sets ORDER BY id"
                    )
                )
            return list(
                connection.execute(
                    """
                    SELECT * FROM benchmark_prediction_sets
                    WHERE session_id = ? ORDER BY id
                    """,
                    (session_id,),
                )
            )

    def list_benchmark_predictions(
        self, prediction_set_id: int, *, prediction_kind: str | None = None
    ) -> list[sqlite3.Row]:
        self.get_benchmark_prediction_set(prediction_set_id)
        sql = "SELECT * FROM benchmark_predictions WHERE prediction_set_id = ?"
        params: list[Any] = [prediction_set_id]
        if prediction_kind is not None:
            sql += " AND prediction_kind = ?"
            params.append(prediction_kind)
        sql += " ORDER BY session_start_ms, session_end_ms, id"
        with self.connect() as connection:
            return list(connection.execute(sql, params))

    def record_benchmark_run(
        self,
        truth_set_id: int,
        prediction_set_id: int,
        *,
        config: dict[str, Any],
        metrics: dict[str, Any],
        details: dict[str, Any],
        report_json_path: str,
        report_markdown_path: str,
    ) -> int:
        self.get_truth_set(truth_set_id)
        self.get_benchmark_prediction_set(prediction_set_id)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO benchmark_runs (
                    truth_set_id, prediction_set_id, config_json,
                    metrics_json, details_json, report_json_path,
                    report_markdown_path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    truth_set_id,
                    prediction_set_id,
                    json.dumps(config, ensure_ascii=False, sort_keys=True),
                    json.dumps(metrics, ensure_ascii=False, sort_keys=True),
                    json.dumps(details, ensure_ascii=False, sort_keys=True),
                    report_json_path,
                    report_markdown_path,
                    utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    def list_benchmark_runs(self, truth_set_id: int) -> list[sqlite3.Row]:
        self.get_truth_set(truth_set_id)
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT br.*, bps.name AS prediction_name,
                           bps.adapter AS prediction_adapter
                    FROM benchmark_runs br
                    JOIN benchmark_prediction_sets bps
                      ON bps.id = br.prediction_set_id
                    WHERE br.truth_set_id = ? ORDER BY br.id
                    """,
                    (truth_set_id,),
                )
            )

    def upsert_action_candidate(self, values: dict[str, Any]) -> sqlite3.Row:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO action_candidates (
                    recording_id, candidate_key, candidate_type, status,
                    start_ms, end_ms, source_segment_ids_json, title,
                    scheduled_at, time_text, location, participants_json,
                    confidence, evidence_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_key) DO UPDATE SET
                    start_ms = excluded.start_ms,
                    end_ms = excluded.end_ms,
                    source_segment_ids_json = excluded.source_segment_ids_json,
                    title = CASE
                        WHEN action_candidates.status = 'pending' THEN excluded.title
                        ELSE action_candidates.title
                    END,
                    scheduled_at = CASE
                        WHEN action_candidates.status = 'pending' THEN excluded.scheduled_at
                        ELSE action_candidates.scheduled_at
                    END,
                    time_text = excluded.time_text,
                    location = CASE
                        WHEN action_candidates.status = 'pending' THEN excluded.location
                        ELSE action_candidates.location
                    END,
                    participants_json = excluded.participants_json,
                    confidence = excluded.confidence,
                    evidence_json = excluded.evidence_json,
                    updated_at = excluded.updated_at
                """,
                (
                    values["recording_id"],
                    values["candidate_key"],
                    values["candidate_type"],
                    values["start_ms"],
                    values["end_ms"],
                    json.dumps(values["source_segment_ids"], ensure_ascii=False),
                    values["title"],
                    values.get("scheduled_at"),
                    values.get("time_text"),
                    values.get("location"),
                    json.dumps(values.get("participants", []), ensure_ascii=False),
                    values["confidence"],
                    json.dumps(values["evidence"], ensure_ascii=False),
                    now,
                    now,
                ),
            )
            return connection.execute(
                "SELECT * FROM action_candidates WHERE candidate_key = ?",
                (values["candidate_key"],),
            ).fetchone()

    def list_action_candidates(
        self, recording_id: int, *, status: str | None = None
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM action_candidates WHERE recording_id = ?"
        params: list[Any] = [recording_id]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY start_ms, id"
        with self.connect() as connection:
            return list(connection.execute(sql, params))

    def get_action_candidate(self, candidate_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM action_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"行动候选 {candidate_id} 不存在")
        return row

    def review_action_candidate(
        self,
        candidate_id: int,
        *,
        status: str,
        title: str | None = None,
        scheduled_at: str | None = None,
        location: str | None = None,
    ) -> sqlite3.Row:
        if status not in {"pending", "confirmed", "dismissed"}:
            raise ValueError("status 只能是 pending、confirmed 或 dismissed")
        if title is not None and not title.strip():
            raise ValueError("title 不能为空")
        if scheduled_at is not None:
            try:
                datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("scheduled_at 必须是有效的 ISO 8601 时间") from exc
        updates = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, utc_now()]
        if title is not None:
            updates.append("title = ?")
            params.append(title.strip())
        if scheduled_at is not None:
            updates.append("scheduled_at = ?")
            params.append(scheduled_at)
        if location is not None:
            updates.append("location = ?")
            params.append(location)
        params.append(candidate_id)
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE action_candidates SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            if cursor.rowcount != 1:
                raise KeyError(f"行动候选 {candidate_id} 不存在")
            return connection.execute(
                "SELECT * FROM action_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
