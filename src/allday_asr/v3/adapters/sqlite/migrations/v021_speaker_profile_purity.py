"""Separate, append-only evidence for clip-level speaker profile purity."""

SQL = """
CREATE TABLE speaker_profile_purity_runs (
  audit_run_id TEXT PRIMARY KEY,
  audit_version TEXT NOT NULL,
  model TEXT NOT NULL,
  model_version TEXT NOT NULL,
  embedding_hash TEXT NOT NULL,
  profile_snapshot_hash TEXT NOT NULL,
  source_snapshot_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE speaker_profile_purity_tasks (
  task_id TEXT PRIMARY KEY,
  audit_run_id TEXT NOT NULL REFERENCES speaker_profile_purity_runs(audit_run_id),
  source_key TEXT NOT NULL UNIQUE,
  target_person_id TEXT NOT NULL REFERENCES persons(person_id),
  source_media_id TEXT NOT NULL,
  start_ms INTEGER NOT NULL,
  end_ms INTEGER NOT NULL CHECK(end_ms > start_ms),
  source_session_id TEXT,
  source_track_id TEXT,
  source_cluster_id TEXT,
  priority TEXT NOT NULL CHECK(priority IN ('P0','P0-special','P1','P2')),
  reason_codes_json TEXT NOT NULL,
  source_snapshot_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_speaker_profile_purity_tasks_priority
ON speaker_profile_purity_tasks(priority, created_at, task_id);
CREATE TABLE speaker_profile_purity_reviews (
  review_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES speaker_profile_purity_tasks(task_id),
  revision INTEGER NOT NULL CHECK(revision > 0),
  primary_speaker_person_id TEXT REFERENCES persons(person_id),
  primary_speaker_unknown INTEGER NOT NULL CHECK(primary_speaker_unknown IN (0,1)),
  purity TEXT CHECK(purity IN ('clean_single','mixed_overlap','boundary_cross','uncertain')),
  other_speaker_ids_json TEXT NOT NULL,
  quality_flags_json TEXT NOT NULL,
  review_source TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('submit','undo')),
  reviewed_at TEXT NOT NULL,
  UNIQUE(task_id, revision)
);
CREATE INDEX idx_speaker_profile_purity_reviews_latest
ON speaker_profile_purity_reviews(task_id, revision DESC);
CREATE TRIGGER protect_speaker_profile_purity_reviews_update
BEFORE UPDATE ON speaker_profile_purity_reviews BEGIN
  SELECT RAISE(ABORT, 'purity review is immutable');
END;
CREATE TRIGGER protect_speaker_profile_purity_reviews_delete
BEFORE DELETE ON speaker_profile_purity_reviews BEGIN
  SELECT RAISE(ABORT, 'purity review cannot be deleted');
END;
"""
