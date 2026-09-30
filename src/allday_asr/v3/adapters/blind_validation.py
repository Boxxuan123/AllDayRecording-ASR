"""Durable shadow validation. No production prototypes or person facts are written."""

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from uuid import uuid4

import numpy as np

from allday_asr.v3.adapters.sqlite.blind_queries import automatic_queries, latest_run
from allday_asr.v3.adapters.sqlite.blind_reviews import phone_tasks, submit_truth
from allday_asr.v3.adapters.sqlite.dataset_reservations import now, settings
from allday_asr.v3.ports.speaker_embeddings import SpeakerClipInput, SpeakerTrackInput
from allday_asr.v3.adapters.sqlite.blind_report import build_report, markdown
from allday_asr.v3.application.blind_scoring import components, digest, encoded, score, unit
from allday_asr.v3.application.query_purity import probe, query_features

LOG = logging.getLogger(__name__)


def verify_model(snapshot):
    from allday_asr.v3.adapters.models.funasr import _cached_model_or_id
    root = Path(_cached_model_or_id('cam++'))
    for filename, expected in snapshot['model_files'].items():
        path = root / filename
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('frozen CAM++ model fingerprint mismatch; new experiment required')


class BlindValidationService:
    def __init__(self, database, provider, output_dir, *, model_verifier=verify_model):
        self.database, self.provider = database, provider
        self.output_dir = Path(output_dir)
        self.model_verifier = model_verifier
        self._stop = threading.Event()
        self._thread = None
        self._start_lock = threading.Lock()
        self._report_lock = threading.Lock()

    def start(self):
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name='blind-shadow-worker', daemon=True)
            self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def initialize_experiment(self, experiment_id, clean_version, snapshot):
        if set(snapshot['legacy']) != set(snapshot['matcher']['known_ids']) or set(snapshot['clean']) != set(snapshot['legacy']):
            raise ValueError('both arms require the same frozen person library')
        for refs in (snapshot['legacy'], snapshot['clean']):
            for vectors in refs.values():
                if not vectors:
                    raise ValueError('missing frozen profile')
                for vector in vectors:
                    unit(vector)
        snapshot_hash = digest(snapshot)
        with self.database.transaction() as c:
            row = c.execute('SELECT snapshot_hash,clean_profile_version FROM blind_experiments WHERE experiment_id=?', (experiment_id,)).fetchone()
            if row:
                if row[0] != snapshot_hash or row[1] != clean_version:
                    raise ValueError('experiment already frozen; choose a new version')
            else:
                if c.execute('SELECT 1 FROM blind_experiments WHERE retired_at IS NULL').fetchone():
                    raise ValueError('retire the active experiment explicitly before a new version')
                c.execute('INSERT INTO blind_experiments VALUES(?,?,?,?,?,NULL)',
                          (experiment_id, clean_version, encoded(snapshot), snapshot_hash, now()))
        self.report(experiment_id)
        return snapshot_hash

    def enqueue(self, session_id):
        with self.database.transaction() as c:
            return self._enqueue(c, session_id)

    def _enqueue(self, c, session_id):
        role = c.execute('SELECT dataset_role,historical_diagnostic_only,role_assigned_at FROM session_dataset_roles WHERE session_id=?', (session_id,)).fetchone()
        experiment = c.execute('SELECT * FROM blind_experiments WHERE retired_at IS NULL ORDER BY created_at DESC LIMIT 1').fetchone()
        if role is None or role[0] != 'blind' or role[1] or experiment is None:
            return False
        run_id = latest_run(c, session_id)
        if not run_id:
            return False
        old = c.execute('SELECT * FROM blind_shadow_jobs WHERE session_id=? AND experiment_id=?', (session_id, experiment['experiment_id'])).fetchone()
        if old and (old['input_revision'] == run_id or old['status'] == 'running'):
            return False
        queries = automatic_queries(c, session_id, experiment['experiment_id'], run_id)
        existing = {r[0] for r in c.execute('SELECT query_id FROM blind_query_views WHERE experiment_id=?', (experiment['experiment_id'],))}
        new_queries = [q for q in queries if q['query_id'] not in existing]
        frozen = json.loads(experiment['snapshot_json'])
        contamination = c.execute("""SELECT 1 FROM annotation_facts f JOIN utterances u ON u.utterance_id=f.source_utterance_id
            WHERE u.session_id=? AND f.dimension='person' LIMIT 1""", (session_id,)).fetchone()
        seen = set(frozen['seen_audio_sha256'])
        old_capture = c.execute('SELECT captured_start FROM recording_sessions WHERE session_id=?', (session_id,)).fetchone()[0] < frozen['data_cutoff']
        error = ('human person facts preceded new prediction' if contamination and new_queries else
                 'historical audio predates experiment' if old_capture else
                 'original media already used before experiment' if any(w['sha256'] in seen for q in new_queries for w in q['windows']) else
                 'no usable automatic speaker queries' if not queries else None)
        stamp = now()
        state = 'blocked' if error else 'queued' if new_queries else 'completed'
        c.execute("""INSERT INTO blind_shadow_jobs(session_id,experiment_id,status,input_revision,inputs_json,updated_at,error)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(session_id,experiment_id) DO UPDATE SET status=excluded.status,
            input_revision=excluded.input_revision,inputs_json=excluded.inputs_json,token=NULL,lease_until=0,
            retry_at=0,error=excluded.error,updated_at=excluded.updated_at""",
            (session_id, experiment['experiment_id'], state, run_id, encoded(new_queries), stamp, error))
        return state == 'queued'

    def _discover(self):
        with self.database.transaction() as c:
            sessions = [r[0] for r in c.execute("""SELECT r.session_id FROM session_dataset_roles r
                WHERE r.dataset_role='blind' AND r.historical_diagnostic_only=0
                  AND EXISTS(SELECT 1 FROM processing_runs p WHERE p.session_id=r.session_id AND p.status='succeeded')""")]
            for session_id in sessions:
                self._enqueue(c, session_id)

    def retry(self, session_id):
        with self.database.transaction() as c:
            c.execute("""UPDATE blind_shadow_jobs SET status='queued',retry_at=0,error=NULL,updated_at=?
                WHERE session_id=? AND status='failed'""", (now(), session_id))

    def run_once(self):
        self._discover()
        token = uuid4().hex
        with self.database.transaction() as c:
            job = c.execute("""SELECT j.*,e.snapshot_json,e.snapshot_hash FROM blind_shadow_jobs j
                JOIN blind_experiments e USING(experiment_id) WHERE e.retired_at IS NULL
                AND ((j.status IN ('queued','failed') AND j.retry_at<=?) OR (j.status='running' AND j.lease_until<?))
                ORDER BY j.updated_at,j.session_id LIMIT 1""", (time.time(), time.time())).fetchone()
            if job is None:
                return False
            job = dict(job)
            c.execute("""UPDATE blind_shadow_jobs SET status='running',attempts=attempts+1,token=?,lease_until=?,updated_at=?
                WHERE session_id=? AND experiment_id=?""", (token, time.time()+3600, now(), job['session_id'], job['experiment_id']))
        try:
            frozen = json.loads(job['snapshot_json'])
            if (self.provider.model, self.provider.model_version) != (frozen['model'], frozen['model_version']):
                raise ValueError('embedding provider differs from frozen experiment')
            self.model_verifier(frozen)
            results = []
            for query in json.loads(job['inputs_json']):
                tracks = tuple(SpeakerTrackInput(f"{query['query_id']}:{i}", query['session_id'],
                    (SpeakerClipInput(w['media_id'], w['storage_key'], w['start_ms'], w['end_ms'], w['utterance_id']),))
                    for i, w in enumerate(query['windows']))
                embeddings = {e.speaker_track_id: e for e in self.provider.embed(tracks)}
                if len(embeddings) != len(tracks):
                    raise ValueError('incomplete automatic query embedding; retry, no silent window omission')
                vectors = []
                for track, window in zip(tracks, query['windows'], strict=True):
                    embedding = embeddings[track.speaker_track_id]
                    if (embedding.model, embedding.model_version) != (frozen['model'], frozen['model_version']) or [
                        (r.media_id, r.start_ms, r.end_ms) for r in embedding.representatives] != [
                        (window['media_id'], window['start_ms'], window['end_ms'])]:
                        raise ValueError('embedding model/ranges changed')
                    vector = unit(embedding.vector)
                    window['embedding'] = list(vector)
                    vectors.append(vector)
                vector = unit(np.mean(vectors, axis=0))
                query['vector'] = list(vector)
                probe_result = probe(query_features(query['windows']), frozen.get('query_probe_thresholds', {}))
                prediction = {'legacy': score(vector, query['duration_s'], frozen['legacy'], frozen['matcher']),
                    'clean': score(vector, query['duration_s'], frozen['clean'], frozen['matcher']),
                    'query_probe': probe_result, 'query_probe_affects_decision': False,
                    'model_hash': frozen['model_sha256'], 'profile_snapshot_hashes': frozen['profile_hashes'],
                    'matcher_version': frozen['matcher_version'], 'GP_version': frozen['GP_version'],
                    'experiment_hash': job['snapshot_hash'], 'duration_s': query['duration_s'],
                    'window_count': query['window_count'], 'session_id': query['session_id'],
                    'speaker_track_id': query['speaker_track_id']}
                results.append((query, prediction))
            with self.database.transaction() as c:
                active = c.execute('SELECT token,status FROM blind_shadow_jobs WHERE session_id=? AND experiment_id=?', (job['session_id'], job['experiment_id'])).fetchone()
                if active[0] != token or active[1] != 'running':
                    return True
                if c.execute("""SELECT 1 FROM annotation_facts f JOIN utterances u ON u.utterance_id=f.source_utterance_id
                    WHERE u.session_id=? AND f.dimension='person' LIMIT 1""", (job['session_id'],)).fetchone():
                    c.execute("UPDATE blind_shadow_jobs SET status='blocked',error='human truth arrived before prediction',updated_at=? WHERE token=?", (now(), token))
                    return True
                stamp = now()
                for query, prediction in results:
                    c.execute('INSERT OR IGNORE INTO blind_query_views VALUES(?,?,?,?,?,?,?)',
                        (query['query_id'], query['session_id'], query['speaker_track_id'], job['experiment_id'], encoded(query), digest(query), stamp))
                    c.execute('INSERT OR IGNORE INTO blind_prediction_snapshots VALUES(?,?,?,?,?)',
                        (digest([job['experiment_id'], query['query_id']]), query['query_id'], job['experiment_id'], encoded(prediction), stamp))
                self._group_events(c, job['experiment_id'])
                c.execute("UPDATE blind_shadow_jobs SET status='completed',error=NULL,token=NULL,lease_until=0,updated_at=? WHERE token=?", (now(), token))
        except Exception as exc:
            LOG.exception('Blind shadow job failed; production ingestion is unaffected')
            with self.database.transaction() as c:
                c.execute("""UPDATE blind_shadow_jobs SET status='failed',error=?,retry_at=?,lease_until=0,updated_at=? WHERE token=?""",
                          (str(exc)[:2000], time.time()+min(3600, 30*2**min(job['attempts'], 7)), now(), token))
        self.report(job['experiment_id'])
        return True

    def _group_events(self, c, experiment_id):
        queries = [json.loads(r[0]) for r in c.execute('SELECT query_json FROM blind_query_views WHERE experiment_id=? ORDER BY created_at,query_id', (experiment_id,))]
        previous = [dict(r) for r in c.execute('SELECT * FROM blind_events WHERE experiment_id=? AND superseded_by IS NULL ORDER BY created_at,event_id', (experiment_id,))]
        for rows, edges in components(queries):
            ids = sorted(q['query_id'] for q in rows)
            event_id = digest([experiment_id, ids])
            if any(e['event_id'] == event_id for e in previous):
                continue
            old = [e for e in previous if set(json.loads(e['query_ids_json'])) & set(ids)]
            # Oldest representative is preserved without consulting scores/truth.
            canonical = old[0]['canonical_query_id'] if old else sorted(rows, key=lambda q: (-q['duration_s'], q['query_id']))[0]['query_id']
            query = next(q for q in rows if q['query_id'] == canonical)
            stamp = now()
            c.execute('INSERT OR IGNORE INTO blind_events VALUES(?,?,?,?,?,?,?,NULL)',
                (event_id, experiment_id, query['session_id'], canonical, encoded(ids), encoded({'rule': 'media-overlap-or-track5s-cluster2s-v1', 'edges': edges}), stamp))
            for event in old:
                c.execute('UPDATE blind_events SET superseded_by=? WHERE event_id=?', (event_id, event['event_id']))
            task_id = digest(['blind-review', experiment_id, canonical])
            prediction = json.loads(c.execute('SELECT prediction_json FROM blind_prediction_snapshots WHERE query_id=?', (canonical,)).fetchone()[0])
            priority = 2 if any(prediction['legacy']['decisions'][r] != prediction['clean']['decisions'][r] for r in ('G', 'P')) else 1
            c.execute('INSERT INTO blind_review_tasks VALUES(?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET event_id=excluded.event_id',
                      (task_id, event_id, canonical, priority, stamp))

    def tasks(self, *, history=False, task_id=None):
        with self.database.read() as c:
            return phone_tasks(c, history=history, task_id=task_id)

    def submit(self, task_id, payload, source):
        with self.database.transaction() as c:
            result = submit_truth(c, task_id, payload, source)
        # Truth remains durable if export fails; worker regenerates exports on startup.
        try:
            self.report()
        except Exception:
            LOG.exception('Blind truth saved; private report export will retry')
        return result

    def report(self, experiment_id=None):
        with self._report_lock:
            with self.database.transaction() as c:
                if experiment_id is None:
                    row = c.execute('SELECT experiment_id FROM blind_experiments ORDER BY created_at DESC LIMIT 1').fetchone()
                    experiment_id = row[0] if row else ''
                built = build_report(c, experiment_id)
            if isinstance(built, dict):
                return built
            report, files = built
            # Keep separate version exports, plus current convenience paths.
            directories = [self.output_dir, self.output_dir / experiment_id]
            for directory in directories:
                directory.mkdir(parents=True, exist_ok=True)
                for filename, value in files.items():
                    temporary = directory / (filename + '.' + uuid4().hex + '.tmp')
                    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
                    temporary.replace(directory / filename)
                temporary = directory / ('current-report.md.' + uuid4().hex + '.tmp')
                temporary.write_text(markdown(report), encoding='utf-8')
                temporary.replace(directory / 'current-report.md')
            return report

    def status(self):
        with self.database.read() as c:
            config = settings(c)
            roles = {r[0]: r[1] for r in c.execute('SELECT dataset_role,COUNT(*) FROM session_dataset_roles GROUP BY dataset_role')}
            jobs = {r[0]: r[1] for r in c.execute('SELECT status,COUNT(*) FROM blind_shadow_jobs GROUP BY status')}
            pending = len(phone_tasks(c))
        return {'blind_collection_mode': bool(config['blind_collection_mode']),
                'next_sessions': 'blind' if config['blind_collection_mode'] else 'deterministic reservation policy',
                'settings': config, 'session_roles': roles, 'shadow_jobs': jobs, 'pending_review': pending}

    def _loop(self):
        last_export = time.monotonic()
        try:
            self.report()
        except Exception:
            LOG.exception('Blind report startup failed')
        while not self._stop.is_set():
            try:
                if self.run_once():
                    continue
                if time.monotonic() - last_export >= 60:
                    self.report()
                    last_export = time.monotonic()
            except Exception:
                LOG.exception('Blind worker will retry independently of ingestion')
            self._stop.wait(10)
