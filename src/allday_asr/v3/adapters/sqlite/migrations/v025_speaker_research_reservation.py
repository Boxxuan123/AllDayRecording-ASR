"""Prospective calendar-day research reservations; historical roles stay frozen."""

SQL = """
CREATE TABLE speaker_research_policy (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 policy_version TEXT NOT NULL, activated_at TEXT NOT NULL,
 historical_max_rowid INTEGER NOT NULL,
 cadence_days INTEGER NOT NULL CHECK(cadence_days=5)
);
INSERT INTO speaker_research_policy SELECT 1,'utc-calendar-speaker-v1',
 strftime('%Y-%m-%dT%H:%M:%fZ','now'),COALESCE(MAX(rowid),0),5 FROM recording_sessions;
CREATE TRIGGER freeze_speaker_research_policy_update BEFORE UPDATE ON speaker_research_policy
BEGIN SELECT RAISE(ABORT,'research allocation policy is frozen'); END;
CREATE TRIGGER freeze_speaker_research_policy_delete BEFORE DELETE ON speaker_research_policy
BEGIN SELECT RAISE(ABORT,'research allocation policy is frozen'); END;

CREATE VIEW prospective_speaker_roles AS
 SELECT s.session_id,date(s.captured_start) AS capture_date_utc,p.policy_version,
 CASE CAST(julianday(date(s.captured_start))-julianday('1970-01-01') AS INTEGER)%5
 WHEN 0 THEN 'independent_evaluation' WHEN 4 THEN 'development' ELSE 'learning' END AS research_role
 FROM recording_sessions s JOIN speaker_research_policy p ON p.singleton=1
 WHERE s.rowid>p.historical_max_rowid AND date(s.captured_start)>date(p.activated_at);

CREATE TABLE session_speaker_reservations (
 session_id TEXT PRIMARY KEY REFERENCES recording_sessions(session_id),
 research_role TEXT NOT NULL CHECK(research_role IN
 ('learning','development','independent_evaluation','excluded_blind','excluded_holdout')),
 capture_date_utc TEXT NOT NULL, reserved_at TEXT NOT NULL,
 first_prediction_at TEXT, policy_version TEXT NOT NULL,
 CHECK(julianday(reserved_at) IS NOT NULL)
);
CREATE TABLE speaker_research_usage (
 session_id TEXT NOT NULL REFERENCES recording_sessions(session_id),
 purpose TEXT NOT NULL CHECK(purpose IN ('enrollment','calibration','profile_learning',
 'threshold_fitting','rule_fitting','model_selection','candidate_design','training',
 'development','diagnostic','blind','product_inference','frozen_prediction','manual_ground_truth','final_evaluation')),
 source_id TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(session_id,purpose,source_id)
);
CREATE TABLE speaker_enrollment_provenance (
 source_sha256 TEXT PRIMARY KEY CHECK(length(source_sha256)=64),
 asset_snapshot_sha256 TEXT NOT NULL, recorded_at TEXT NOT NULL
);
CREATE TRIGGER freeze_enrollment_provenance_update BEFORE UPDATE ON speaker_enrollment_provenance
BEGIN SELECT RAISE(ABORT,'enrollment provenance is immutable'); END;
CREATE TRIGGER freeze_enrollment_provenance_delete BEFORE DELETE ON speaker_enrollment_provenance
BEGIN SELECT RAISE(ABORT,'enrollment provenance is immutable'); END;

DROP TRIGGER reserve_session_dataset_role;
CREATE TRIGGER reserve_session_dataset_role AFTER INSERT ON recording_sessions BEGIN
 UPDATE dataset_reservation_settings SET next_ordinal=next_ordinal+1 WHERE singleton=1;
 INSERT INTO session_dataset_roles
 SELECT NEW.session_id,
 CASE WHEN EXISTS(SELECT 1 FROM prospective_speaker_roles p WHERE p.session_id=NEW.session_id
                 AND p.research_role IN ('development','independent_evaluation')) THEN 'holdout'
      WHEN blind_collection_mode=1 THEN 'blind'
      WHEN ((next_ordinal*6181)%10000)/10000.0<blind_ratio THEN 'blind'
      WHEN ((next_ordinal*6181)%10000)/10000.0<blind_ratio+holdout_ratio THEN 'holdout'
      ELSE 'learning' END,
 strftime('%Y-%m-%dT%H:%M:%fZ','now'),
 CASE WHEN EXISTS(SELECT 1 FROM prospective_speaker_roles p WHERE p.session_id=NEW.session_id
                 AND p.research_role IN ('development','independent_evaluation'))
      THEN 'utc-calendar-speaker-v1' ELSE policy_version END,1,
 CASE WHEN EXISTS(SELECT 1 FROM prospective_speaker_roles p WHERE p.session_id=NEW.session_id
                 AND p.research_role IN ('development','independent_evaluation'))
      OR blind_collection_mode=1 OR ((next_ordinal*6181)%10000)/10000.0<blind_ratio+holdout_ratio
      THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE NULL END,
 next_ordinal,0,json_object('policy_version',policy_version,'settings_revision',revision,
 'blind_ratio',blind_ratio,'holdout_ratio',holdout_ratio,'blind_collection_mode',blind_collection_mode,
 'research_policy','utc-calendar-speaker-v1')
 FROM dataset_reservation_settings WHERE singleton=1;
 INSERT INTO session_role_audit SELECT session_id,1,dataset_role,role_assignment_policy,'reservation',role_assigned_at
 FROM session_dataset_roles WHERE session_id=NEW.session_id;
END;
CREATE TRIGGER reserve_speaker_research AFTER INSERT ON session_dataset_roles BEGIN
 INSERT INTO session_speaker_reservations
 SELECT NEW.session_id,
 CASE WHEN p.research_role IN ('development','independent_evaluation') THEN p.research_role
      WHEN NEW.dataset_role='learning' THEN 'learning'
      WHEN NEW.dataset_role='blind' THEN 'excluded_blind' ELSE 'excluded_holdout' END,
 p.capture_date_utc,NEW.role_assigned_at,NULL,p.policy_version
 FROM prospective_speaker_roles p WHERE p.session_id=NEW.session_id;
END;
CREATE TRIGGER freeze_speaker_reservation_delete BEFORE DELETE ON session_speaker_reservations
BEGIN SELECT RAISE(ABORT,'research reservation is permanent'); END;
CREATE TRIGGER freeze_speaker_reservation_update BEFORE UPDATE ON session_speaker_reservations
WHEN NEW.session_id!=OLD.session_id OR NEW.research_role!=OLD.research_role
 OR NEW.capture_date_utc!=OLD.capture_date_utc OR NEW.reserved_at!=OLD.reserved_at
 OR NEW.policy_version!=OLD.policy_version
 OR (OLD.first_prediction_at IS NOT NULL AND NEW.first_prediction_at IS NOT OLD.first_prediction_at)
 OR (NEW.first_prediction_at IS NOT NULL AND (julianday(NEW.first_prediction_at) IS NULL
     OR julianday(NEW.first_prediction_at)<=julianday(NEW.reserved_at)))
BEGIN SELECT RAISE(ABORT,'research reservation/prediction timestamp is frozen'); END;
CREATE TRIGGER freeze_research_capture_date BEFORE UPDATE OF captured_start ON recording_sessions
WHEN EXISTS(SELECT 1 FROM session_speaker_reservations r WHERE r.session_id=OLD.session_id
 AND r.capture_date_utc IS NOT date(NEW.captured_start))
BEGIN SELECT RAISE(ABORT,'reserved capture date cannot change'); END;
CREATE TRIGGER block_independent_research_use BEFORE INSERT ON speaker_research_usage
WHEN EXISTS(SELECT 1 FROM session_speaker_reservations r WHERE r.session_id=NEW.session_id
 AND r.research_role='independent_evaluation')
 AND NEW.purpose NOT IN ('product_inference','frozen_prediction','manual_ground_truth','final_evaluation')
BEGIN SELECT RAISE(ABORT,'independent evaluation cannot be used for development or learning'); END;
CREATE TRIGGER freeze_research_usage_update BEFORE UPDATE ON speaker_research_usage
BEGIN SELECT RAISE(ABORT,'research usage is immutable'); END;
CREATE TRIGGER freeze_research_usage_delete BEFORE DELETE ON speaker_research_usage
BEGIN SELECT RAISE(ABORT,'research usage is immutable'); END;
CREATE TRIGGER block_research_learning_exposure BEFORE INSERT ON session_learning_exposure
WHEN EXISTS(SELECT 1 FROM session_speaker_reservations r WHERE r.session_id=NEW.session_id
 AND r.research_role IN ('development','independent_evaluation'))
BEGIN SELECT RAISE(ABORT,'research reservation excludes learning exposure'); END;
CREATE TRIGGER block_reserved_profile_clip_update BEFORE UPDATE OF representative_clips_json ON voice_prototypes
WHEN NEW.status='accepted' AND EXISTS (
 SELECT 1 FROM json_each(NEW.representative_clips_json) clip
 JOIN audio_assets a ON a.media_id=json_extract(clip.value,'$.media_id')
 JOIN capture_segments s USING(asset_id) JOIN session_dataset_roles r USING(session_id)
 WHERE r.dataset_role!='learning')
BEGIN SELECT RAISE(ABORT,'dataset_role_excluded: reserved profile source'); END;
"""
