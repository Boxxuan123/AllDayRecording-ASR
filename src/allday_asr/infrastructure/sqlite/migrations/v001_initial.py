"""Initial schema for migration version 1."""

SQL = """
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
