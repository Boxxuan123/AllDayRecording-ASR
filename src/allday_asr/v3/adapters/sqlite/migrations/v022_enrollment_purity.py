"""Additive source ledger and bounded candidate queue; no production profile writes."""

SQL = """
CREATE TABLE speaker_purity_sources (
  source_key TEXT PRIMARY KEY,
  source_media_id TEXT NOT NULL,
  start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
  end_ms INTEGER NOT NULL CHECK(end_ms > start_ms),
  UNIQUE(source_media_id,start_ms,end_ms)
);
CREATE TABLE speaker_source_purity_evidence (
  evidence_id TEXT PRIMARY KEY,
  source_key TEXT NOT NULL REFERENCES speaker_purity_sources(source_key),
  purity TEXT NOT NULL CHECK(purity IN ('clean_single','mixed_overlap','boundary_cross',
    'wrong_primary','uncertain','unreviewed')),
  review_primary_person_id TEXT REFERENCES persons(person_id),
  conflicting INTEGER NOT NULL CHECK(conflicting IN (0,1)),
  review_references_json TEXT NOT NULL,
  provenance_json TEXT NOT NULL,
  reconciled_at TEXT NOT NULL
);
CREATE TABLE speaker_purity_current (
  source_key TEXT PRIMARY KEY REFERENCES speaker_purity_sources(source_key),
  evidence_id TEXT NOT NULL REFERENCES speaker_source_purity_evidence(evidence_id)
);
CREATE TABLE speaker_purity_targets (
  source_key TEXT NOT NULL REFERENCES speaker_purity_sources(source_key),
  target_person_id TEXT NOT NULL REFERENCES persons(person_id),
  PRIMARY KEY(source_key,target_person_id)
);
CREATE TABLE purity_candidates (
  candidate_id TEXT PRIMARY KEY,
  source_key TEXT NOT NULL REFERENCES speaker_purity_sources(source_key),
  target_person_id TEXT NOT NULL REFERENCES persons(person_id),
  source_session_id TEXT,
  candidate_kind TEXT NOT NULL CHECK(candidate_kind IN ('enrollment','recrop_candidate')),
  parent_source_key TEXT REFERENCES speaker_purity_sources(source_key),
  status TEXT NOT NULL CHECK(status IN ('unreviewed','reviewed_clean',
    'reviewed_rejected','not_needed','superseded')),
  risk_rank INTEGER NOT NULL,
  provenance_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(source_key,target_person_id)
);
CREATE INDEX idx_purity_candidate_queue ON purity_candidates(status,risk_rank DESC);
CREATE TRIGGER protect_source_purity_evidence_update
BEFORE UPDATE ON speaker_source_purity_evidence BEGIN
  SELECT RAISE(ABORT, 'source purity evidence is immutable');
END;
CREATE TRIGGER protect_source_purity_evidence_delete
BEFORE DELETE ON speaker_source_purity_evidence BEGIN
  SELECT RAISE(ABORT, 'source purity evidence cannot be deleted');
END;
"""
