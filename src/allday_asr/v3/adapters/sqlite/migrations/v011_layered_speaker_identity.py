"""Layered known-person matching and human-confirmed prototype learning."""

SQL = """
CREATE TABLE person_identity_policy_revisions (
    person_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    maturity_status TEXT NOT NULL CHECK(maturity_status IN (
        'seed', 'learning', 'calibrated', 'suspended'
    )),
    auto_match_enabled INTEGER NOT NULL CHECK(auto_match_enabled IN (0, 1)),
    suggest_threshold REAL NOT NULL CHECK(
        suggest_threshold >= -1 AND suggest_threshold <= 1
    ),
    auto_accept_threshold REAL NOT NULL CHECK(
        auto_accept_threshold >= -1 AND auto_accept_threshold <= 1
    ),
    minimum_margin REAL NOT NULL CHECK(
        minimum_margin >= 0 AND minimum_margin <= 1
    ),
    minimum_quality REAL NOT NULL CHECK(
        minimum_quality >= 0 AND minimum_quality <= 1
    ),
    calibration_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(person_id, revision),
    CHECK(auto_accept_threshold >= suggest_threshold),
    CHECK(auto_match_enabled = 0 OR maturity_status = 'calibrated'),
    FOREIGN KEY(person_id) REFERENCES persons(person_id) ON DELETE RESTRICT
);

INSERT INTO person_identity_policy_revisions (
    person_id, revision, maturity_status, auto_match_enabled,
    suggest_threshold, auto_accept_threshold, minimum_margin,
    minimum_quality, calibration_json, actor, created_at
)
SELECT person_id, 1, 'seed', 0, 0.82, 0.92, 0.05, 0.50, '{}',
  'system:v37-identity-policy-bootstrap', created_at
FROM persons;

CREATE INDEX idx_person_identity_policy_current
ON person_identity_policy_revisions(person_id, revision DESC);

CREATE TABLE voice_prototype_reviews (
    review_id TEXT PRIMARY KEY,
    prototype_id TEXT NOT NULL,
    person_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN (
        'confirmed', 'rejected', 'uncertain', 'retracted'
    )),
    actor TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(prototype_id) REFERENCES voice_prototypes(prototype_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(person_id) REFERENCES persons(person_id) ON DELETE RESTRICT
);

CREATE INDEX idx_voice_prototype_reviews_current
ON voice_prototype_reviews(prototype_id, person_id, created_at DESC, review_id DESC);

CREATE INDEX idx_voice_prototype_reviews_person
ON voice_prototype_reviews(person_id, decision, created_at DESC, review_id DESC);

CREATE TABLE speaker_match_decisions (
    decision_id TEXT PRIMARY KEY,
    cluster_id TEXT NOT NULL,
    prototype_id TEXT NOT NULL,
    speaker_track_id TEXT NOT NULL,
    decision_tier TEXT NOT NULL CHECK(decision_tier IN (
        'insufficient_evidence', 'auto_matched', 'suggested', 'no_known_match'
    )),
    candidate_person_id TEXT,
    best_score REAL CHECK(best_score IS NULL OR (best_score >= -1 AND best_score <= 1)),
    second_best_score REAL CHECK(
        second_best_score IS NULL OR
        (second_best_score >= -1 AND second_best_score <= 1)
    ),
    score_margin REAL CHECK(score_margin IS NULL OR score_margin >= 0),
    quality_score REAL NOT NULL CHECK(quality_score >= 0 AND quality_score <= 1),
    policy_revision INTEGER,
    policy_version TEXT NOT NULL,
    reason TEXT NOT NULL,
    trigger TEXT NOT NULL CHECK(trigger IN (
        'initial_analysis', 'historical_rematch', 'prototype_review',
        'policy_change'
    )),
    created_at TEXT NOT NULL,
    CHECK(
        (candidate_person_id IS NULL AND policy_revision IS NULL) OR
        (candidate_person_id IS NOT NULL AND policy_revision IS NOT NULL)
    ),
    FOREIGN KEY(cluster_id) REFERENCES speaker_clusters(cluster_id) ON DELETE RESTRICT,
    FOREIGN KEY(prototype_id) REFERENCES voice_prototypes(prototype_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(speaker_track_id) REFERENCES speaker_tracks(speaker_track_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(candidate_person_id) REFERENCES persons(person_id) ON DELETE RESTRICT,
    FOREIGN KEY(candidate_person_id, policy_revision)
        REFERENCES person_identity_policy_revisions(person_id, revision)
        ON DELETE RESTRICT
);

CREATE INDEX idx_speaker_match_decisions_current
ON speaker_match_decisions(prototype_id, created_at DESC, decision_id DESC);

CREATE INDEX idx_speaker_match_review_queue
ON speaker_match_decisions(
    decision_tier, candidate_person_id, created_at DESC, decision_id DESC
);

CREATE TRIGGER protect_person_identity_policy_revisions_from_update
BEFORE UPDATE ON person_identity_policy_revisions BEGIN
    SELECT RAISE(ABORT, 'person identity policy revision is immutable');
END;

CREATE TRIGGER protect_person_identity_policy_revisions_from_delete
BEFORE DELETE ON person_identity_policy_revisions BEGIN
    SELECT RAISE(ABORT, 'person identity policy revision cannot be deleted');
END;

CREATE TRIGGER protect_voice_prototype_reviews_from_update
BEFORE UPDATE ON voice_prototype_reviews BEGIN
    SELECT RAISE(ABORT, 'voice prototype review is immutable');
END;

CREATE TRIGGER protect_voice_prototype_reviews_from_delete
BEFORE DELETE ON voice_prototype_reviews BEGIN
    SELECT RAISE(ABORT, 'voice prototype review cannot be deleted');
END;

CREATE TRIGGER protect_speaker_match_decisions_from_update
BEFORE UPDATE ON speaker_match_decisions BEGIN
    SELECT RAISE(ABORT, 'speaker match decision is immutable');
END;

CREATE TRIGGER protect_speaker_match_decisions_from_delete
BEFORE DELETE ON speaker_match_decisions BEGIN
    SELECT RAISE(ABORT, 'speaker match decision cannot be deleted');
END;
"""
