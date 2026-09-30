"""Atomic session reservation, immutable experiments/truth and durable shadow jobs."""

SQL = """
CREATE TABLE dataset_reservation_settings (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 policy_version TEXT NOT NULL, blind_ratio REAL NOT NULL CHECK(blind_ratio>=0 AND blind_ratio<=1),
 holdout_ratio REAL NOT NULL CHECK(holdout_ratio>=0 AND holdout_ratio<=1),
 blind_collection_mode INTEGER NOT NULL CHECK(blind_collection_mode IN (0,1)),
 next_ordinal INTEGER NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL,
 CHECK(blind_ratio+holdout_ratio<=1)
);
INSERT INTO dataset_reservation_settings VALUES(1,'session-counter-v1',0.25,0.05,0,0,1,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
CREATE TABLE dataset_setting_audit (
 revision INTEGER PRIMARY KEY, settings_json TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL
);
INSERT INTO dataset_setting_audit SELECT revision,json_object('policy_version',policy_version,
 'blind_ratio',blind_ratio,'holdout_ratio',holdout_ratio,'blind_collection_mode',blind_collection_mode),
 'migration',updated_at FROM dataset_reservation_settings;
CREATE TABLE session_dataset_roles (
 session_id TEXT PRIMARY KEY REFERENCES recording_sessions(session_id),
 dataset_role TEXT NOT NULL CHECK(dataset_role IN ('learning','blind','holdout')),
 role_assigned_at TEXT NOT NULL, role_assignment_policy TEXT NOT NULL,
 role_revision INTEGER NOT NULL CHECK(role_revision>0), frozen_at TEXT,
 allocation_ordinal INTEGER UNIQUE, historical_diagnostic_only INTEGER NOT NULL DEFAULT 0,
 assignment_settings_json TEXT NOT NULL
);
CREATE TABLE session_role_audit (
 session_id TEXT NOT NULL REFERENCES recording_sessions(session_id),
 role_revision INTEGER NOT NULL, dataset_role TEXT NOT NULL, policy TEXT NOT NULL,
 actor TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(session_id,role_revision)
);
CREATE TABLE session_learning_exposure (
 session_id TEXT NOT NULL REFERENCES recording_sessions(session_id),
 exposure_kind TEXT NOT NULL, source_id TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(session_id,exposure_kind,source_id)
);
CREATE TABLE holdout_openings (
 session_id TEXT NOT NULL REFERENCES recording_sessions(session_id),
 decision_version TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(session_id,decision_version)
);
INSERT INTO session_dataset_roles
 SELECT session_id,'learning',strftime('%Y-%m-%dT%H:%M:%fZ','now'),'historical-pre-reservation',1,NULL,NULL,1,'{}'
 FROM recording_sessions;
INSERT INTO session_learning_exposure SELECT session_id,'historical_pre_reservation',session_id,
 strftime('%Y-%m-%dT%H:%M:%fZ','now') FROM recording_sessions;
INSERT INTO session_role_audit SELECT session_id,1,'learning','historical-pre-reservation','migration',role_assigned_at FROM session_dataset_roles;
CREATE TRIGGER reserve_session_dataset_role AFTER INSERT ON recording_sessions BEGIN
 UPDATE dataset_reservation_settings SET next_ordinal=next_ordinal+1 WHERE singleton=1;
 INSERT INTO session_dataset_roles
 SELECT NEW.session_id,
 CASE WHEN blind_collection_mode=1 THEN 'blind'
      WHEN ((next_ordinal*6181)%10000)/10000.0 < blind_ratio THEN 'blind'
      WHEN ((next_ordinal*6181)%10000)/10000.0 < blind_ratio+holdout_ratio THEN 'holdout'
      ELSE 'learning' END,
 strftime('%Y-%m-%dT%H:%M:%fZ','now'),policy_version,1,
 CASE WHEN blind_collection_mode=1 OR ((next_ordinal*6181)%10000)/10000.0 < blind_ratio+holdout_ratio
      THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE NULL END,
 next_ordinal,0,json_object('policy_version',policy_version,'settings_revision',revision,
 'blind_ratio',blind_ratio,'holdout_ratio',holdout_ratio,'blind_collection_mode',blind_collection_mode)
 FROM dataset_reservation_settings WHERE singleton=1;
 INSERT INTO session_role_audit SELECT session_id,1,dataset_role,role_assignment_policy,'reservation',role_assigned_at
 FROM session_dataset_roles WHERE session_id=NEW.session_id;
END;
CREATE TRIGGER prevent_late_blind_role BEFORE UPDATE ON session_dataset_roles
WHEN NEW.dataset_role!=OLD.dataset_role AND (
 OLD.dataset_role IN ('blind','holdout') OR EXISTS (
 SELECT 1 FROM session_learning_exposure e WHERE e.session_id=OLD.session_id) OR EXISTS (
 SELECT 1 FROM annotation_sample_sets s WHERE s.session_id=OLD.session_id) OR EXISTS (
 SELECT 1 FROM voice_prototypes p JOIN speaker_tracks t USING(speaker_track_id) WHERE t.session_id=OLD.session_id) OR EXISTS (
 SELECT 1 FROM purity_candidates p WHERE p.source_session_id=OLD.session_id) OR EXISTS (
 SELECT 1 FROM annotation_facts f JOIN utterances u ON u.utterance_id=f.source_utterance_id
 WHERE u.session_id=OLD.session_id AND f.dimension='person')) BEGIN
 SELECT RAISE(ABORT,'cannot_promote_to_blind: existing learning, truth, or frozen reservation');
END;
CREATE TRIGGER protect_dataset_role_delete BEFORE DELETE ON session_dataset_roles BEGIN
 SELECT RAISE(ABORT,'dataset reservation cannot be deleted'); END;
CREATE TRIGGER validate_dataset_role_revision BEFORE UPDATE ON session_dataset_roles
WHEN NEW.role_revision!=OLD.role_revision+1 OR NEW.session_id!=OLD.session_id
 OR NEW.role_assigned_at!=OLD.role_assigned_at OR NEW.allocation_ordinal IS NOT OLD.allocation_ordinal
 OR NEW.historical_diagnostic_only!=OLD.historical_diagnostic_only
 OR (NEW.dataset_role!='learning' AND NEW.frozen_at IS NULL)
BEGIN SELECT RAISE(ABORT,'dataset role revision/audit required'); END;
CREATE TRIGGER audit_dataset_role_revision AFTER UPDATE ON session_dataset_roles BEGIN
 INSERT INTO session_role_audit VALUES(NEW.session_id,NEW.role_revision,NEW.dataset_role,
 NEW.role_assignment_policy,'explicit-admin',strftime('%Y-%m-%dT%H:%M:%fZ','now')); END;
CREATE TRIGGER block_reserved_sample_queue BEFORE INSERT ON annotation_sample_queue
WHEN NOT EXISTS(SELECT 1 FROM session_dataset_roles r WHERE r.session_id=NEW.session_id AND r.dataset_role='learning')
BEGIN SELECT RAISE(IGNORE); END;
CREATE TRIGGER block_reserved_sample_queue_update BEFORE UPDATE ON annotation_sample_queue
WHEN NOT EXISTS(SELECT 1 FROM session_dataset_roles r WHERE r.session_id=NEW.session_id AND r.dataset_role='learning')
BEGIN SELECT RAISE(IGNORE); END;
CREATE TRIGGER block_reserved_sample_set BEFORE INSERT ON annotation_sample_sets
WHEN NOT EXISTS(SELECT 1 FROM session_dataset_roles r WHERE r.session_id=NEW.session_id AND r.dataset_role='learning')
BEGIN SELECT RAISE(ABORT,'dataset_role_excluded: sample generation'); END;
CREATE TRIGGER block_reserved_sample_selection BEFORE UPDATE OF current ON annotation_sample_sets
WHEN NEW.current=1 AND NOT EXISTS(SELECT 1 FROM session_dataset_roles r WHERE r.session_id=NEW.session_id AND r.dataset_role='learning')
BEGIN SELECT RAISE(ABORT,'dataset_role_excluded: sample selection'); END;
CREATE TRIGGER record_sample_learning_exposure AFTER INSERT ON annotation_sample_sets BEGIN
 INSERT OR IGNORE INTO session_learning_exposure VALUES(NEW.session_id,'annotation_sample',NEW.sample_key,strftime('%Y-%m-%dT%H:%M:%fZ','now')); END;
CREATE TRIGGER block_reserved_profile_admission BEFORE INSERT ON voice_prototypes
WHEN NEW.status='accepted' AND (NOT EXISTS(SELECT 1 FROM speaker_tracks t JOIN session_dataset_roles r USING(session_id)
 WHERE t.speaker_track_id=NEW.speaker_track_id AND r.dataset_role='learning') OR EXISTS (
 SELECT 1 FROM json_each(NEW.representative_clips_json) clip JOIN audio_assets a ON a.media_id=json_extract(clip.value,'$.media_id')
 JOIN capture_segments s USING(asset_id) JOIN session_dataset_roles r USING(session_id) WHERE r.dataset_role!='learning'))
BEGIN SELECT RAISE(ABORT,'dataset_role_excluded: profile admission'); END;
CREATE TRIGGER block_reserved_profile_update BEFORE UPDATE OF status,person_id,speaker_track_id,representative_clips_json ON voice_prototypes
WHEN NEW.status='accepted' AND NOT EXISTS(SELECT 1 FROM speaker_tracks t JOIN session_dataset_roles r USING(session_id)
 WHERE t.speaker_track_id=NEW.speaker_track_id AND r.dataset_role='learning')
BEGIN SELECT RAISE(ABORT,'dataset_role_excluded: profile update'); END;
CREATE TRIGGER record_prototype_learning_exposure AFTER INSERT ON voice_prototypes
WHEN EXISTS(SELECT 1 FROM speaker_tracks t JOIN session_dataset_roles r USING(session_id)
 WHERE t.speaker_track_id=NEW.speaker_track_id AND r.dataset_role='learning') BEGIN
 INSERT OR IGNORE INTO session_learning_exposure SELECT session_id,'prototype',NEW.prototype_id,
 strftime('%Y-%m-%dT%H:%M:%fZ','now') FROM speaker_tracks WHERE speaker_track_id=NEW.speaker_track_id; END;
CREATE TRIGGER block_reserved_purity_candidate BEFORE INSERT ON purity_candidates
WHEN NOT EXISTS(SELECT 1 FROM session_dataset_roles r WHERE r.session_id=NEW.source_session_id AND r.dataset_role='learning')
 OR EXISTS(SELECT 1 FROM speaker_purity_sources p JOIN audio_assets a ON a.media_id=p.source_media_id
 JOIN capture_segments s USING(asset_id) JOIN session_dataset_roles r USING(session_id)
 WHERE p.source_key=NEW.source_key AND r.dataset_role!='learning')
BEGIN SELECT RAISE(ABORT,'dataset_role_excluded: enrollment purity candidate'); END;
CREATE TRIGGER block_reserved_purity_update BEFORE UPDATE OF source_session_id,source_key ON purity_candidates
WHEN NOT EXISTS(SELECT 1 FROM session_dataset_roles r WHERE r.session_id=NEW.source_session_id AND r.dataset_role='learning')
BEGIN SELECT RAISE(ABORT,'dataset_role_excluded: enrollment purity candidate'); END;
CREATE TABLE blind_experiments (
 experiment_id TEXT PRIMARY KEY, clean_profile_version TEXT NOT NULL,
 snapshot_json TEXT NOT NULL, snapshot_hash TEXT NOT NULL,
 created_at TEXT NOT NULL, retired_at TEXT
);
CREATE TABLE blind_shadow_jobs (
 session_id TEXT NOT NULL REFERENCES recording_sessions(session_id),
 experiment_id TEXT NOT NULL REFERENCES blind_experiments(experiment_id),
 status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','blocked')),
 input_revision TEXT NOT NULL, inputs_json TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, token TEXT, lease_until REAL NOT NULL DEFAULT 0,
 retry_at REAL NOT NULL DEFAULT 0, error TEXT, updated_at TEXT NOT NULL,
 PRIMARY KEY(session_id,experiment_id)
);
CREATE TABLE blind_query_views (
 query_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES recording_sessions(session_id),
 speaker_track_id TEXT NOT NULL, experiment_id TEXT NOT NULL REFERENCES blind_experiments(experiment_id),
 query_json TEXT NOT NULL, query_hash TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE blind_prediction_snapshots (
 prediction_snapshot_id TEXT PRIMARY KEY, query_id TEXT NOT NULL REFERENCES blind_query_views(query_id),
 experiment_id TEXT NOT NULL REFERENCES blind_experiments(experiment_id),
 prediction_json TEXT NOT NULL, prediction_created_at TEXT NOT NULL,
 UNIQUE(query_id,experiment_id)
);
CREATE TABLE blind_events (
 event_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL REFERENCES blind_experiments(experiment_id),
 session_id TEXT NOT NULL REFERENCES recording_sessions(session_id), canonical_query_id TEXT NOT NULL,
 query_ids_json TEXT NOT NULL, grouping_json TEXT NOT NULL, created_at TEXT NOT NULL,
 superseded_by TEXT REFERENCES blind_events(event_id)
);
CREATE TABLE blind_review_tasks (
 task_id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES blind_events(event_id),
 query_id TEXT NOT NULL REFERENCES blind_query_views(query_id), priority INTEGER NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(event_id,query_id)
);
CREATE TABLE blind_ground_truth (
 review_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES blind_review_tasks(task_id),
 revision INTEGER NOT NULL CHECK(revision>0), action TEXT NOT NULL CHECK(action IN ('submit','undo')),
 primary_person_id TEXT REFERENCES persons(person_id), unknown_kind TEXT NOT NULL
 CHECK(unknown_kind IN ('none','stranger','dont_know','inaudible')),
 purity TEXT CHECK(purity IN ('clean_single','mixed_overlap','boundary_cross','uncertain')),
 review_source TEXT NOT NULL, reviewed_at TEXT NOT NULL, UNIQUE(task_id,revision)
);
CREATE TABLE blind_evaluations (
 evaluation_id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES blind_events(event_id),
 truth_revision_hash TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(event_id,truth_revision_hash)
);
CREATE TRIGGER protect_blind_prediction_update BEFORE UPDATE ON blind_prediction_snapshots BEGIN
 SELECT RAISE(ABORT,'blind prediction is frozen'); END;
CREATE TRIGGER protect_blind_prediction_delete BEFORE DELETE ON blind_prediction_snapshots BEGIN
 SELECT RAISE(ABORT,'blind prediction cannot be deleted'); END;
CREATE TRIGGER protect_blind_truth_update BEFORE UPDATE ON blind_ground_truth BEGIN
 SELECT RAISE(ABORT,'blind truth is immutable'); END;
CREATE TRIGGER protect_blind_truth_delete BEFORE DELETE ON blind_ground_truth BEGIN
 SELECT RAISE(ABORT,'blind truth cannot be deleted'); END;
CREATE TRIGGER protect_blind_experiment_update BEFORE UPDATE OF snapshot_json,snapshot_hash ON blind_experiments BEGIN
 SELECT RAISE(ABORT,'blind experiment is frozen'); END;
CREATE TRIGGER protect_blind_experiment_delete BEFORE DELETE ON blind_experiments BEGIN
 SELECT RAISE(ABORT,'blind experiment cannot be deleted'); END;
CREATE TRIGGER protect_blind_query_update BEFORE UPDATE ON blind_query_views BEGIN
 SELECT RAISE(ABORT,'blind query is frozen'); END;
CREATE TRIGGER protect_blind_query_delete BEFORE DELETE ON blind_query_views BEGIN
 SELECT RAISE(ABORT,'blind query cannot be deleted'); END;
CREATE TRIGGER protect_blind_version BEFORE UPDATE OF experiment_id,clean_profile_version,created_at ON blind_experiments BEGIN
 SELECT RAISE(ABORT,'blind experiment version is frozen'); END;
CREATE TRIGGER truth_after_prediction BEFORE INSERT ON blind_ground_truth
WHEN NOT EXISTS(SELECT 1 FROM blind_review_tasks t JOIN blind_prediction_snapshots p USING(query_id)
 WHERE t.task_id=NEW.task_id AND p.prediction_created_at<NEW.reviewed_at)
BEGIN SELECT RAISE(ABORT,'prediction must precede human truth'); END;
"""
