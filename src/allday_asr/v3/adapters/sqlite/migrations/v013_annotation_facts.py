"""Narrow human fact ledger. Audio windows, never whole media files, are anchors."""

SQL = """
CREATE TABLE annotation_facts (
 fact_id TEXT PRIMARY KEY,
 dimension TEXT NOT NULL CHECK(dimension IN ('person', 'sound')),
 value_json TEXT NOT NULL,
 source_utterance_id TEXT NOT NULL,
 source_revision INTEGER NOT NULL,
 actor TEXT NOT NULL,
 created_at TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('active', 'superseded', 'revoked', 'conflict')),
 payload_json TEXT NOT NULL
);
CREATE TABLE annotation_fact_audio (
 fact_id TEXT NOT NULL REFERENCES annotation_facts(fact_id),
 ordinal INTEGER NOT NULL,
 media_id TEXT NOT NULL,
 start_ms INTEGER NOT NULL,
 end_ms INTEGER NOT NULL CHECK(end_ms > start_ms),
 PRIMARY KEY(fact_id, ordinal)
);
CREATE INDEX idx_annotation_audio ON annotation_fact_audio(media_id, start_ms, end_ms);
CREATE TABLE annotation_supersessions (
 predecessor_id TEXT NOT NULL REFERENCES annotation_facts(fact_id),
 successor_id TEXT NOT NULL REFERENCES annotation_facts(fact_id),
 PRIMARY KEY(predecessor_id, successor_id)
);
CREATE TABLE annotation_sample_revocations (
 prototype_id TEXT NOT NULL REFERENCES voice_prototypes(prototype_id),
 fact_id TEXT NOT NULL REFERENCES annotation_facts(fact_id),
 reason TEXT NOT NULL,
 PRIMARY KEY(prototype_id, fact_id)
);
CREATE TRIGGER annotation_facts_history_fields BEFORE UPDATE OF dimension,value_json,
 source_utterance_id,source_revision,actor,created_at,payload_json ON annotation_facts BEGIN
 SELECT RAISE(ABORT,'human fact history is immutable');
END;
CREATE TRIGGER annotation_facts_history_delete BEFORE DELETE ON annotation_facts BEGIN
 SELECT RAISE(ABORT,'human fact history cannot be deleted');
END;
CREATE TRIGGER annotation_audio_update BEFORE UPDATE ON annotation_fact_audio BEGIN
 SELECT RAISE(ABORT,'human audio anchor is immutable');
END;
CREATE TRIGGER annotation_audio_delete BEFORE DELETE ON annotation_fact_audio BEGIN
 SELECT RAISE(ABORT,'human audio anchor cannot be deleted');
END;
CREATE TRIGGER annotation_supersession_update BEFORE UPDATE ON annotation_supersessions BEGIN
 SELECT RAISE(ABORT,'human supersession is immutable');
END;
CREATE TRIGGER annotation_supersession_delete BEFORE DELETE ON annotation_supersessions BEGIN
 SELECT RAISE(ABORT,'human supersession cannot be deleted');
END;
CREATE TRIGGER annotation_revocation_update BEFORE UPDATE ON annotation_sample_revocations BEGIN
 SELECT RAISE(ABORT,'sample revocation is immutable');
END;
CREATE TRIGGER annotation_revocation_delete BEFORE DELETE ON annotation_sample_revocations BEGIN
 SELECT RAISE(ABORT,'sample revocation cannot be deleted');
END;
"""
