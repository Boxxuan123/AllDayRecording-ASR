"""A queued job already covers subsequent edits until a worker claims it.

Generation is an invalidation epoch, not a count of utterance writes. The first
edit of a running job advances it, so the old worker still cannot publish.
"""

SQL = """
DROP TRIGGER annotation_sample_utterance_insert;
DROP TRIGGER annotation_sample_utterance_update;
DROP TRIGGER annotation_sample_fact_change;
CREATE TRIGGER annotation_sample_utterance_insert AFTER INSERT ON utterances
 WHEN json_type(NEW.evidence_json,'$.annotation_fact_ids')='object' BEGIN
 INSERT INTO annotation_sample_queue(session_id) VALUES(NEW.session_id)
 ON CONFLICT(session_id) DO UPDATE SET generation=generation+1,status='queued',attempts=0,retry_at=0
 WHERE status!='queued' OR attempts!=0 OR retry_at!=0;
END;
CREATE TRIGGER annotation_sample_utterance_update AFTER UPDATE OF evidence_json,status ON utterances BEGIN
 INSERT INTO annotation_sample_queue(session_id) VALUES(NEW.session_id)
 ON CONFLICT(session_id) DO UPDATE SET generation=generation+1,status='queued',attempts=0,retry_at=0
 WHERE status!='queued' OR attempts!=0 OR retry_at!=0;
END;
CREATE TRIGGER annotation_sample_fact_change AFTER UPDATE OF state ON annotation_facts BEGIN
 INSERT INTO annotation_sample_queue(session_id)
 SELECT session_id FROM utterances WHERE utterance_id=NEW.source_utterance_id
 ON CONFLICT(session_id) DO UPDATE SET generation=generation+1,status='queued',attempts=0,retry_at=0
 WHERE status!='queued' OR attempts!=0 OR retry_at!=0;
END;
"""
