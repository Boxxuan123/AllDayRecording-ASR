"""Durable, bounded sample processing; facts remain in the v13 ledger."""

SQL = """
CREATE TABLE annotation_sample_queue (
 session_id TEXT PRIMARY KEY REFERENCES recording_sessions(session_id),
 generation INTEGER NOT NULL DEFAULT 1,
 status TEXT NOT NULL DEFAULT 'queued',
 attempts INTEGER NOT NULL DEFAULT 0,
 retry_at REAL NOT NULL DEFAULT 0,
 lease_until REAL NOT NULL DEFAULT 0,
 token TEXT,
 reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE annotation_sample_sets (
 sample_key TEXT PRIMARY KEY,
 session_id TEXT NOT NULL,
 person_id TEXT NOT NULL,
 model TEXT NOT NULL,
 model_version TEXT NOT NULL,
 prototype_id TEXT NOT NULL REFERENCES voice_prototypes(prototype_id),
 facts_json TEXT NOT NULL,
 windows_json TEXT NOT NULL,
 current INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE annotation_sample_runtime (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), model_key TEXT NOT NULL
);
INSERT OR IGNORE INTO annotation_sample_queue(session_id)
 SELECT DISTINCT u.session_id FROM annotation_facts f JOIN utterances u ON u.utterance_id=f.source_utterance_id;
CREATE TRIGGER annotation_sample_utterance_insert AFTER INSERT ON utterances
 WHEN json_type(NEW.evidence_json,'$.annotation_fact_ids')='object' BEGIN
 INSERT INTO annotation_sample_queue(session_id) VALUES(NEW.session_id)
 ON CONFLICT(session_id) DO UPDATE SET generation=generation+1,status='queued',attempts=0,retry_at=0;
END;
CREATE TRIGGER annotation_sample_utterance_update AFTER UPDATE OF evidence_json,status ON utterances BEGIN
 INSERT INTO annotation_sample_queue(session_id) VALUES(NEW.session_id)
 ON CONFLICT(session_id) DO UPDATE SET generation=generation+1,status='queued',attempts=0,retry_at=0;
END;
CREATE TRIGGER annotation_sample_fact_change AFTER UPDATE OF state ON annotation_facts BEGIN
 INSERT INTO annotation_sample_queue(session_id)
 SELECT session_id FROM utterances WHERE utterance_id=NEW.source_utterance_id
 ON CONFLICT(session_id) DO UPDATE SET generation=generation+1,status='queued',attempts=0,retry_at=0;
END;
"""
