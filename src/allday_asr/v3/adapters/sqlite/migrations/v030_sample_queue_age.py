"""Private worker age metrics and dirty dates for annotated identity context."""
SQL = """
ALTER TABLE annotation_sample_queue ADD COLUMN queued_at REAL NOT NULL DEFAULT 0;
UPDATE annotation_sample_queue SET queued_at=(julianday('now')-2440587.5)*86400;
CREATE TRIGGER annotation_sample_age_insert AFTER INSERT ON annotation_sample_queue
WHEN NEW.queued_at=0 BEGIN
 UPDATE annotation_sample_queue SET queued_at=(julianday('now')-2440587.5)*86400
 WHERE session_id=NEW.session_id;
END;
DROP TRIGGER annotation_sample_utterance_insert;
DROP TRIGGER annotation_sample_utterance_update;
DROP TRIGGER annotation_sample_fact_change;
CREATE TRIGGER annotation_sample_utterance_insert AFTER INSERT ON utterances
 WHEN json_type(NEW.evidence_json,'$.annotation_fact_ids')='object' BEGIN
 INSERT INTO annotation_sample_queue(session_id,queued_at) VALUES(NEW.session_id,(julianday('now')-2440587.5)*86400)
 ON CONFLICT(session_id) DO UPDATE SET generation=generation+1,status='queued',attempts=0,retry_at=0,queued_at=excluded.queued_at
 WHERE status!='queued' OR attempts!=0 OR retry_at!=0;
END;
CREATE TRIGGER annotation_sample_utterance_update AFTER UPDATE OF evidence_json,status ON utterances
 WHEN NEW.evidence_json!=OLD.evidence_json OR NEW.status!=OLD.status
   OR NEW.text!=OLD.text OR NEW.speaker_track_id IS NOT OLD.speaker_track_id BEGIN
 INSERT INTO annotation_sample_queue(session_id,queued_at) VALUES(NEW.session_id,(julianday('now')-2440587.5)*86400)
 ON CONFLICT(session_id) DO UPDATE SET generation=generation+1,status='queued',attempts=0,retry_at=0,queued_at=excluded.queued_at
 WHERE status!='queued' OR attempts!=0 OR retry_at!=0;
END;
CREATE TRIGGER annotation_sample_fact_change AFTER UPDATE OF state ON annotation_facts BEGIN
 INSERT INTO annotation_sample_queue(session_id,queued_at)
 SELECT session_id,(julianday('now')-2440587.5)*86400 FROM utterances WHERE utterance_id=NEW.source_utterance_id
 ON CONFLICT(session_id) DO UPDATE SET generation=generation+1,status='queued',attempts=0,retry_at=0,queued_at=excluded.queued_at
 WHERE status!='queued' OR attempts!=0 OR retry_at!=0;
END;
CREATE TRIGGER daily_dirty_annotated_person_update AFTER UPDATE OF revision ON persons BEGIN
 INSERT INTO daily_dirty_ranges(first_date,last_date,reason)
 SELECT DISTINCT date(u.start_at,'+8 hours'),date(u.start_at,'+8 hours'),'annotated_person_revision'
 FROM utterances u WHERE u.status='active'
 AND json_extract(u.evidence_json,'$.person_annotation.person_id')=NEW.person_id;
END;
"""
