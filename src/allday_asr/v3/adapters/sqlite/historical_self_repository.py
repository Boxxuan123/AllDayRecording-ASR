"""Read product eligibility without reading research ground truth."""
import json


class HistoricalSelfRepository:
    def __init__(self, connection):
        self.connection = connection

    def sessions(self, since=None, until=None, limit=100, session_id=None):
        return [dict(r) for r in self.connection.execute('''SELECT s.session_id,s.captured_start
          FROM recording_sessions s WHERE s.tombstoned_at IS NULL
          AND (? IS NULL OR julianday(s.captured_start)>=julianday(?))
          AND (? IS NULL OR julianday(s.captured_start)<julianday(?))
          AND (? IS NULL OR s.session_id=?) ORDER BY s.captured_start DESC,s.session_id LIMIT ?''',
          (since,since,until,until,session_id,session_id,limit))]

    def session_skip(self, sid):
        c = self.connection
        role = c.execute('SELECT dataset_role FROM session_dataset_roles WHERE session_id=?', (sid,)).fetchone()
        research = c.execute('SELECT research_role FROM session_speaker_reservations WHERE session_id=?', (sid,)).fetchone()
        if research and research[0] == 'independent_evaluation':
            return 'INDEPENDENT_EVALUATION'
        if research and research[0] != 'learning':
            return 'FROZEN_EXPERIMENT'
        if role is None:
            return 'MISSING_RESERVATION'
        if role[0] != 'learning':
            return 'FROZEN_EXPERIMENT'
        if c.execute('''SELECT 1 FROM capture_segments s JOIN audio_assets a USING(asset_id)
          JOIN speaker_enrollment_provenance p ON p.source_sha256=a.sha256 WHERE s.session_id=? LIMIT 1''', (sid,)).fetchone():
            return 'ENROLLMENT_SOURCE'
        # Frozen predictions must remain excluded even if an old role was anomalous.
        if c.execute('SELECT 1 FROM blind_prediction_snapshots p JOIN blind_query_views q USING(query_id) WHERE q.session_id=? LIMIT 1', (sid,)).fetchone():
            return 'FROZEN_EXPERIMENT'
        return None

    def utterance_skip(self, uid):
        c = self.connection
        row = c.execute('SELECT identity,status,evidence_json,speaker_track_id FROM utterances WHERE utterance_id=?', (uid,)).fetchone()
        if not row or row['status'] != 'active':
            return 'INACTIVE'
        if json.loads(row['evidence_json']).get('person_annotation'):
            return 'KNOWN_PERSON'
        if c.execute('''SELECT 1 FROM speaker_cluster_memberships m JOIN person_cluster_links p USING(cluster_id)
          WHERE m.speaker_track_id=? AND m.state='active' AND p.status='active' LIMIT 1''',
          (row['speaker_track_id'],)).fetchone():
            return 'KNOWN_PERSON'
        for correction in c.execute('SELECT actor,patch_json FROM correction_operations WHERE target_type=\'utterance\' AND target_id=?', (uid,)):
            if not correction['actor'].startswith('system:') and 'identity' in json.loads(correction['patch_json']):
                return 'HUMAN_OVERRIDE'
        if row['identity'] != 'unknown':
            return 'STABLE_IDENTITY'
        # A previously presented item remains suppressed after uncertain/reject.
        if self.exists() and c.execute("SELECT 1 FROM historical_self_items WHERE utterance_id=? AND decision='REVIEW_SELF_CANDIDATE'", (uid,)).fetchone():
            return 'REVIEW_ALREADY_PRESENTED'
        return None

    def exists(self):
        return self.connection.execute("SELECT 1 FROM sqlite_master WHERE name='historical_self_runs'").fetchone() is not None

    def run(self, run_id):
        if not self.exists():
            return None
        row = self.connection.execute('SELECT result_json FROM historical_self_runs WHERE run_id=?', (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def record_run(self, plan, result, now):
        self.connection.execute('INSERT INTO historical_self_runs VALUES (?,?,?,?)',
            (plan['run_id'],json.dumps(plan,ensure_ascii=False),json.dumps(result),now))

    def record_item(self, run_id, item, now):
        self.connection.execute('INSERT INTO historical_self_items VALUES (?,?,?,?,?,?,?,?)',
            (item['item_id'],run_id,item['utterance_id'],item['session_id'],item['revision'],
             item['decision'],json.dumps(item,ensure_ascii=False),now))

    def review_rows(self, item_id=None):
        if not self.exists():
            return []
        return [dict(r) for r in self.connection.execute('''SELECT i.*,u.text,u.start_at,u.identity,u.revision,u.status,
          r.action,r.result_json,r.operation_id,r.actor
          FROM historical_self_items i JOIN utterances u USING(utterance_id)
          LEFT JOIN historical_self_reviews r USING(item_id)
          WHERE i.decision='REVIEW_SELF_CANDIDATE' AND (? IS NULL OR i.item_id=?)
          ORDER BY i.created_at,i.item_id''', (item_id,item_id))]

    def prior_review(self, operation_id):
        if not self.exists():
            return None
        r = self.connection.execute('SELECT * FROM historical_self_reviews WHERE operation_id=?', (operation_id,)).fetchone()
        return dict(r) if r else None

    def record_review(self, item_id, operation_id, action, actor, result, now):
        self.connection.execute('INSERT INTO historical_self_reviews VALUES (?,?,?,?,?,?)',
            (item_id,operation_id,action,actor,json.dumps(result),now))
