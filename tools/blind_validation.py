"""Freeze a versioned experiment once; background jobs/report then run automatically."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from allday_asr.v3.bootstrap import compose_v3_core  # noqa: E402
from allday_asr.v3.application.blind_scoring import digest  # noqa: E402
from allday_asr.v3.adapters.sqlite.dataset_reservations import now  # noqa: E402


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def freeze(core, args):
    with core.database.read() as c:
        existing = c.execute('SELECT * FROM blind_experiments WHERE experiment_id=?', (args.experiment,)).fetchone()
    if existing:
        snapshot = json.loads(existing['snapshot_json'])
        requested = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (args.clean_profile,args.model_lock,args.matcher)}
        if (requested != snapshot['input_sha256'] or existing['clean_profile_version'] != args.clean_version
            or (read(args.probe_thresholds) if args.probe_thresholds else {}) != snapshot['query_probe_thresholds']):
            raise ValueError('experiment already frozen with different inputs; choose a new version')
        return existing['snapshot_hash']
    clean = read(args.clean_profile)
    lock = read(args.model_lock)
    matcher = read(args.matcher)
    known = set(matcher['known_ids'])
    refs = {}
    for center in clean['centers']:
        if center['person_id'] in known:
            refs.setdefault(center['person_id'], []).append(center['vector'])
    with core.database.read() as c:
        # Same legacy representation/source graph as the previous frozen audit.
        from analyze_speaker_profile_purity import refs_current
        graph_row = c.execute('SELECT source_snapshot_json FROM speaker_profile_purity_runs ORDER BY created_at DESC LIMIT 1').fetchone()
        if not graph_row:
            raise ValueError('legacy purity audit snapshot is required')
        graph = json.loads(graph_row[0])
        legacy = {p: [v.tolist() for v in vectors] for p, vectors in refs_current(c, graph, known).items()}
        for row in graph:
            role = c.execute('SELECT dataset_role FROM session_dataset_roles WHERE session_id=?', (row['source_session_id'],)).fetchone()
            if role is None or role[0] != 'learning':
                raise ValueError('legacy snapshot contains non-learning session')
        for center in clean['centers']:
            role = c.execute('SELECT dataset_role FROM session_dataset_roles WHERE session_id=?', (center['session_id'],)).fetchone()
            if role is None or role[0] != 'learning':
                raise ValueError('clean snapshot contains non-learning session')
        seen = [r[0] for r in c.execute('SELECT sha256 FROM audio_assets ORDER BY sha256')]
        self_row = c.execute("SELECT person_id FROM persons WHERE kind='self'").fetchone()
        self_id = self_row[0] if self_row else None
    if clean['model_fingerprint'] != lock['fingerprint'] or lock['frozen_matcher'] != matcher:
        raise ValueError('clean model/matcher lock differs from frozen configuration')
    files = {k: v for k, v in lock['model_files'].items() if k in {'campplus_cn_common.bin', 'config.yaml', 'configuration.json'}}
    snapshot = {'model': clean['model'], 'model_version': clean['model_version'],
        'model_files': files, 'model_sha256': matcher['model_sha256'], 'matcher': matcher,
        'matcher_version': 'max-session-centroid-cosine-v1', 'GP_version': digest(matcher),
        'legacy': legacy, 'clean': refs, 'profile_hashes': {'legacy': digest(legacy), 'clean': digest(refs)},
        'self_person_id': self_id, 'seen_audio_sha256': seen, 'data_cutoff': now(),
        'query_probe_thresholds': read(args.probe_thresholds) if args.probe_thresholds else {},
        'evidence_requirements': {'events': 30, 'sessions': 5, 'dates': 3,
            'events_per_known': 3, 'sessions_per_known': 2, 'unknown_events': 10, 'self_events': 5},
        'input_sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in (args.clean_profile, args.model_lock, args.matcher)}}
    core.blind_validation.model_verifier(snapshot)
    version_dir = core.blind_validation.output_dir / args.experiment
    version_dir.mkdir(parents=True, exist_ok=True)
    for filename, value in (('frozen-clean-profile.json',clean),('frozen-legacy-source-graph.json',graph),
                            ('frozen-model-lock.json',lock),('frozen-matcher.json',matcher)):
        path = version_dir / filename
        if path.exists() and read(path) != value:
            raise ValueError('local frozen provenance already exists with different inputs')
        path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    return core.blind_validation.initialize_experiment(args.experiment, args.clean_version, snapshot)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['initialize', 'report', 'retry', 'work-once', 'retire'])
    parser.add_argument('--experiment', default='blind-experiment-v1')
    parser.add_argument('--clean-version', default='clean-profile-v1')
    parser.add_argument('--clean-profile', type=Path, default=ROOT/'outputs/enrollment-purity-gate-20260930/shadow-profile.json')
    parser.add_argument('--model-lock', type=Path, default=ROOT/'outputs/enrollment-purity-gate-20260930/model-lock.json')
    parser.add_argument('--matcher', type=Path, default=ROOT/'outputs/speaker-identity-blind-validation-20260928/frozen-candidates.json')
    parser.add_argument('--probe-thresholds', type=Path)
    parser.add_argument('--session')
    args = parser.parse_args()
    core = compose_v3_core()
    core.initialize()
    try:
        if args.command == 'initialize':
            print('Frozen experiment:', freeze(core, args))
        elif args.command == 'retry':
            if not args.session:
                parser.error('retry requires --session')
            core.blind_validation.retry(args.session)
        elif args.command == 'work-once':
            core.blind_validation.run_once()
        elif args.command == 'retire':
            with core.database.transaction() as c:
                c.execute('UPDATE blind_experiments SET retired_at=COALESCE(retired_at,?) WHERE experiment_id=?', (now(), args.experiment))
        report = core.blind_validation.report(args.experiment)
        print(json.dumps({'status': report['status'], 'progress': report.get('progress', {}),
                          'report_path': str(core.blind_validation.output_dir/'current-report.json')}, indent=2))
    finally:
        core.close()


if __name__ == '__main__':
    main()
