"""Capture affected Singapore dates on source writes; historical repair stays explicit."""

SQL = """
CREATE TABLE daily_dirty_ranges (
    dirty_id INTEGER PRIMARY KEY AUTOINCREMENT,
    first_date TEXT NOT NULL,
    last_date TEXT NOT NULL,
    reason TEXT NOT NULL,
    CHECK(first_date <= last_date)
);
CREATE INDEX idx_daily_dirty_ranges_id ON daily_dirty_ranges(dirty_id);
CREATE INDEX idx_daily_utterance_start ON utterances(julianday(start_at)) WHERE status='active';
CREATE INDEX idx_daily_session_start ON recording_sessions(julianday(captured_start));
CREATE INDEX idx_daily_session_end ON recording_sessions(julianday(captured_end));
CREATE INDEX idx_daily_event_local_date
ON event_current_states(json_extract(payload_json,'$.local_date'))
WHERE json_extract(payload_json,'$.daily_event_version') IS NOT NULL;

-- A migration seeds only the two recent dates. It never schedules a historical replay.
INSERT INTO daily_dirty_ranges(first_date,last_date,reason)
VALUES (date('now','+8 hours','-1 day'),date('now','+8 hours'),'migration_recent');

CREATE TRIGGER daily_dirty_session_insert AFTER INSERT ON recording_sessions BEGIN
    INSERT INTO daily_dirty_ranges(first_date,last_date,reason)
    VALUES (date(NEW.captured_start,'+8 hours'),
            MAX(date(NEW.captured_start,'+8 hours'),date(COALESCE(NEW.captured_end,datetime('now')),'+8 hours')),'session');
END;
CREATE TRIGGER daily_dirty_session_update AFTER UPDATE ON recording_sessions BEGIN
    INSERT INTO daily_dirty_ranges(first_date,last_date,reason)
    VALUES (date(OLD.captured_start,'+8 hours'),
            MAX(date(OLD.captured_start,'+8 hours'),date(COALESCE(OLD.captured_end,datetime('now')),'+8 hours')),'session_old');
    INSERT INTO daily_dirty_ranges(first_date,last_date,reason)
    VALUES (date(NEW.captured_start,'+8 hours'),
            MAX(date(NEW.captured_start,'+8 hours'),date(COALESCE(NEW.captured_end,datetime('now')),'+8 hours')),'session_new');
END;

CREATE TRIGGER daily_dirty_utterance_insert AFTER INSERT ON utterances BEGIN
    INSERT INTO daily_dirty_ranges(first_date,last_date,reason)
    VALUES (date(NEW.start_at,'+8 hours'),date(NEW.start_at,'+8 hours'),'utterance');
END;
CREATE TRIGGER daily_dirty_utterance_update AFTER UPDATE ON utterances BEGIN
    INSERT INTO daily_dirty_ranges(first_date,last_date,reason)
    VALUES (date(OLD.start_at,'+8 hours'),date(OLD.start_at,'+8 hours'),'utterance_old');
    INSERT INTO daily_dirty_ranges(first_date,last_date,reason)
    VALUES (date(NEW.start_at,'+8 hours'),date(NEW.start_at,'+8 hours'),'utterance_new');
END;

CREATE TRIGGER daily_dirty_processing_insert AFTER INSERT ON processing_runs BEGIN
    INSERT INTO daily_dirty_ranges(first_date,last_date,reason)
    SELECT date(s.captured_start,'+8 hours'),
           MAX(date(s.captured_start,'+8 hours'),date(COALESCE(s.captured_end,datetime('now')),'+8 hours')),'processing'
    FROM recording_sessions s WHERE s.session_id=NEW.session_id;
END;
CREATE TRIGGER daily_dirty_processing_update AFTER UPDATE ON processing_runs BEGIN
    INSERT INTO daily_dirty_ranges(first_date,last_date,reason)
    SELECT date(s.captured_start,'+8 hours'),
           MAX(date(s.captured_start,'+8 hours'),date(COALESCE(s.captured_end,datetime('now')),'+8 hours')),'processing'
    FROM recording_sessions s WHERE s.session_id=NEW.session_id;
END;
CREATE TRIGGER daily_dirty_segment_insert AFTER INSERT ON capture_segments BEGIN
    INSERT INTO daily_dirty_ranges(first_date,last_date,reason)
    SELECT date(s.captured_start,'+8 hours'),
           MAX(date(s.captured_start,'+8 hours'),date(COALESCE(s.captured_end,datetime('now')),'+8 hours')),'segment'
    FROM recording_sessions s WHERE s.session_id=NEW.session_id;
END;
CREATE TRIGGER daily_dirty_segment_update AFTER UPDATE ON capture_segments BEGIN
    INSERT INTO daily_dirty_ranges(first_date,last_date,reason)
    SELECT date(s.captured_start,'+8 hours'),
           MAX(date(s.captured_start,'+8 hours'),date(COALESCE(s.captured_end,datetime('now')),'+8 hours')),
           'segment'
    FROM recording_sessions s WHERE s.session_id=NEW.session_id OR s.session_id=OLD.session_id;
END;
CREATE TRIGGER daily_dirty_manifest_insert AFTER INSERT ON session_manifests BEGIN
    INSERT INTO daily_dirty_ranges(first_date,last_date,reason)
    SELECT date(s.captured_start,'+8 hours'),
           MAX(date(s.captured_start,'+8 hours'),date(COALESCE(s.captured_end,datetime('now')),'+8 hours')),'manifest'
    FROM recording_sessions s WHERE s.session_id=NEW.session_id;
END;
CREATE TRIGGER daily_dirty_manifest_update AFTER UPDATE ON session_manifests BEGIN
    INSERT INTO daily_dirty_ranges(first_date,last_date,reason)
    SELECT date(s.captured_start,'+8 hours'),
           MAX(date(s.captured_start,'+8 hours'),date(COALESCE(s.captured_end,datetime('now')),'+8 hours')),
           'manifest'
    FROM recording_sessions s WHERE s.session_id=NEW.session_id OR s.session_id=OLD.session_id;
END;

CREATE TRIGGER daily_dirty_event_insert AFTER INSERT ON event_current_states BEGIN
    INSERT INTO daily_dirty_ranges(first_date,last_date,reason)
    SELECT date(NEW.payload_json ->> '$.local_date'),
           date(NEW.payload_json ->> '$.local_date'),'daily_event'
    WHERE NEW.payload_json ->> '$.local_date' IS NOT NULL;
    INSERT INTO daily_dirty_ranges(first_date,last_date,reason)
    SELECT DISTINCT date(u.start_at,'+8 hours'),date(u.start_at,'+8 hours'),'task_event'
    FROM evidence_links l JOIN utterances u ON u.utterance_id=l.evidence_id
    WHERE l.subject_type='event' AND l.subject_id=NEW.event_id
      AND l.evidence_type='utterance';
END;
CREATE TRIGGER daily_dirty_event_update AFTER UPDATE ON event_current_states BEGIN
    INSERT INTO daily_dirty_ranges(first_date,last_date,reason)
    SELECT date(OLD.payload_json ->> '$.local_date'),
           date(OLD.payload_json ->> '$.local_date'),'daily_event_old'
    WHERE OLD.payload_json ->> '$.local_date' IS NOT NULL;
    INSERT INTO daily_dirty_ranges(first_date,last_date,reason)
    SELECT date(NEW.payload_json ->> '$.local_date'),
           date(NEW.payload_json ->> '$.local_date'),'daily_event'
    WHERE NEW.payload_json ->> '$.local_date' IS NOT NULL;
    INSERT INTO daily_dirty_ranges(first_date,last_date,reason)
    SELECT DISTINCT date(u.start_at,'+8 hours'),date(u.start_at,'+8 hours'),'task_event'
    FROM evidence_links l JOIN utterances u ON u.utterance_id=l.evidence_id
    WHERE l.subject_type='event' AND l.subject_id=NEW.event_id
      AND l.evidence_type='utterance';
END;
CREATE TRIGGER daily_dirty_evidence_link_update AFTER UPDATE ON evidence_links BEGIN
    INSERT INTO daily_dirty_ranges(first_date,last_date,reason)
    SELECT date(u.start_at,'+8 hours'),date(u.start_at,'+8 hours'),'event_evidence'
    FROM utterances u WHERE u.utterance_id IN (NEW.evidence_id,OLD.evidence_id)
      AND (NEW.subject_type='event' OR OLD.subject_type='event');
END;
CREATE TRIGGER daily_dirty_evidence_link_delete AFTER DELETE ON evidence_links
WHEN OLD.subject_type='event' AND OLD.evidence_type='utterance' BEGIN
    INSERT INTO daily_dirty_ranges(first_date,last_date,reason)
    SELECT date(u.start_at,'+8 hours'),date(u.start_at,'+8 hours'),'event_evidence'
    FROM utterances u WHERE u.utterance_id=OLD.evidence_id;
END;
CREATE TRIGGER daily_dirty_evidence_link_insert AFTER INSERT ON evidence_links
WHEN NEW.subject_type='event' AND NEW.evidence_type='utterance' BEGIN
    INSERT INTO daily_dirty_ranges(first_date,last_date,reason)
    SELECT date(u.start_at,'+8 hours'),date(u.start_at,'+8 hours'),'event_evidence'
    FROM utterances u WHERE u.utterance_id=NEW.evidence_id;
END;

CREATE TRIGGER daily_dirty_summary_insert AFTER INSERT ON daily_summary_revisions BEGIN
    INSERT INTO daily_dirty_ranges(first_date,last_date,reason)
    VALUES (NEW.summary_date,NEW.summary_date,'summary');
END;

-- Identity metadata is part of the daily source fingerprint. Mark only dates
-- whose active utterances refer to the changed cluster/person.
CREATE TRIGGER daily_dirty_membership_insert AFTER INSERT ON speaker_cluster_memberships BEGIN
    INSERT INTO daily_dirty_ranges(first_date,last_date,reason)
    SELECT DISTINCT date(u.start_at,'+8 hours'),date(u.start_at,'+8 hours'),'membership'
    FROM utterances u WHERE u.speaker_track_id=NEW.speaker_track_id AND u.status='active';
END;
CREATE TRIGGER daily_dirty_membership_update AFTER UPDATE ON speaker_cluster_memberships BEGIN
    INSERT INTO daily_dirty_ranges(first_date,last_date,reason)
    SELECT DISTINCT date(u.start_at,'+8 hours'),date(u.start_at,'+8 hours'),'membership'
    FROM utterances u WHERE u.speaker_track_id=OLD.speaker_track_id AND u.status='active';
    INSERT INTO daily_dirty_ranges(first_date,last_date,reason)
    SELECT DISTINCT date(u.start_at,'+8 hours'),date(u.start_at,'+8 hours'),'membership'
    FROM utterances u WHERE u.speaker_track_id=NEW.speaker_track_id AND u.status='active';
END;
CREATE TRIGGER daily_dirty_person_link_insert AFTER INSERT ON person_cluster_links BEGIN
    INSERT INTO daily_dirty_ranges(first_date,last_date,reason)
    SELECT DISTINCT date(u.start_at,'+8 hours'),date(u.start_at,'+8 hours'),'person_link'
    FROM speaker_cluster_memberships m JOIN utterances u
      ON u.speaker_track_id=m.speaker_track_id
    WHERE m.cluster_id=NEW.cluster_id AND m.state='active' AND u.status='active';
END;
CREATE TRIGGER daily_dirty_person_link_update AFTER UPDATE ON person_cluster_links BEGIN
    INSERT INTO daily_dirty_ranges(first_date,last_date,reason)
    SELECT DISTINCT date(u.start_at,'+8 hours'),date(u.start_at,'+8 hours'),'person_link'
    FROM speaker_cluster_memberships m JOIN utterances u
      ON u.speaker_track_id=m.speaker_track_id
    WHERE m.cluster_id IN (OLD.cluster_id,NEW.cluster_id)
      AND m.state='active' AND u.status='active';
END;
CREATE TRIGGER daily_dirty_person_update AFTER UPDATE OF revision ON persons BEGIN
    INSERT INTO daily_dirty_ranges(first_date,last_date,reason)
    SELECT DISTINCT date(u.start_at,'+8 hours'),date(u.start_at,'+8 hours'),'person_revision'
    FROM person_cluster_links p JOIN speaker_cluster_memberships m
      ON m.cluster_id=p.cluster_id JOIN utterances u
      ON u.speaker_track_id=m.speaker_track_id
    WHERE p.person_id=NEW.person_id AND p.status='active'
      AND m.state='active' AND u.status='active';
END;
"""
