"""Product-only ledger, independent of annotation learning and research truth."""
SQL = """
-- Product identity-only corrections are not annotation/sample inputs.
-- Other content, speaker, evidence and lifecycle edits still invalidate samples.
DROP TRIGGER annotation_sample_utterance_update;
CREATE TRIGGER annotation_sample_utterance_update AFTER UPDATE OF evidence_json,status ON utterances
 WHEN NEW.evidence_json!=OLD.evidence_json OR NEW.status!=OLD.status
   OR NEW.text!=OLD.text OR NEW.speaker_track_id IS NOT OLD.speaker_track_id
 BEGIN
 INSERT INTO annotation_sample_queue(session_id) VALUES(NEW.session_id)
 ON CONFLICT(session_id) DO UPDATE SET generation=generation+1,status='queued',attempts=0,retry_at=0
 WHERE status!='queued' OR attempts!=0 OR retry_at!=0;
END;
DROP TRIGGER sample_revision_utterances_update;
CREATE TRIGGER sample_revision_utterances_update AFTER UPDATE ON utterances
 WHEN NEW.evidence_json!=OLD.evidence_json OR NEW.status!=OLD.status OR NEW.text!=OLD.text
   OR NEW.speaker_track_id IS NOT OLD.speaker_track_id OR NEW.start_ms!=OLD.start_ms OR NEW.end_ms!=OLD.end_ms
   OR NEW.run_id!=OLD.run_id OR NEW.session_id!=OLD.session_id OR NEW.source_artifact_id!=OLD.source_artifact_id
 BEGIN UPDATE annotation_input_revision SET revision=revision+1 WHERE singleton=1; END;
CREATE TABLE historical_self_runs (
 run_id TEXT PRIMARY KEY, plan_json TEXT NOT NULL, result_json TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE historical_self_items (
 item_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES historical_self_runs(run_id),
 utterance_id TEXT NOT NULL REFERENCES utterances(utterance_id), session_id TEXT NOT NULL,
 source_revision INTEGER NOT NULL, decision TEXT NOT NULL,
 trace_json TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(run_id,utterance_id)
);
CREATE UNIQUE INDEX historical_self_pending_once ON historical_self_items(utterance_id)
 WHERE decision='REVIEW_SELF_CANDIDATE';
CREATE TABLE historical_self_reviews (
 item_id TEXT PRIMARY KEY REFERENCES historical_self_items(item_id),
 operation_id TEXT NOT NULL UNIQUE, action TEXT NOT NULL CHECK(action IN ('confirm','reject','uncertain')),
 actor TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER freeze_historical_self_item_update BEFORE UPDATE ON historical_self_items
 BEGIN SELECT RAISE(ABORT,'backfill evidence is immutable'); END;
CREATE TRIGGER freeze_historical_self_item_delete BEFORE DELETE ON historical_self_items
 BEGIN SELECT RAISE(ABORT,'backfill evidence is immutable'); END;
CREATE TRIGGER freeze_historical_self_review_update BEFORE UPDATE ON historical_self_reviews
 BEGIN SELECT RAISE(ABORT,'identity review is immutable'); END;
CREATE TRIGGER freeze_historical_self_review_delete BEFORE DELETE ON historical_self_reviews
 BEGIN SELECT RAISE(ABORT,'identity review is immutable'); END;
"""
