"""Freeze human-reviewed source boundaries without automatic ownership selection."""
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from historical_self_integrity import snapshot
from short_self_dataset import assets, calibration_ranges, digest, read, readonly, write

FORMAT = "pure-self-human-audit-v1"


def anonymous(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:16]


def overlaps(a, b):
    return (a['sha256'] == b['sha256'] and a['start_ms'] < b['end_ms']
            and b['start_ms'] < a['end_ms'])


def strict_review(row, self_id):
    """A batch person annotation alone never proves clean composition."""
    refs = json.loads(row['review_references_json'])
    person = row['review_primary_person_id']
    if (row['purity'] != 'clean_single' or row['conflicting'] or not person or not refs
            or any(r.get('action') != 'submit' or r.get('purity') != 'clean_single'
                   or r.get('primary_person_id') != person for r in refs)):
        return None
    return 'self' if person == self_id else 'non-self'


def leak_flags(event, enrollment, calibration, profiles, fitting):
    return {
        'enrollment': event['sha256'] in enrollment,
        'calibration_positive': any(overlaps(event, r) and r['identity'] == 'self' for r in calibration),
        'calibration_negative': any(overlaps(event, r) and r['identity'] == 'not_self' for r in calibration),
        'profile_learning': any(overlaps(event, r) for r in profiles),
        'prior_diagnostic_fitting': any(overlaps(event, r) for r in fitting),
        'profile_source_file_exposure': any(event['sha256'] == r['sha256'] for r in profiles),
        'fitting_source_file_exposure': any(event['sha256'] == r['sha256'] for r in fitting),
        'calibration_source_file_exposure': any(event['sha256'] == r['sha256'] for r in calibration),
    }


def cohort_for(flags):
    if flags['enrollment'] or flags['calibration_positive'] or flags['calibration_negative']:
        return None
    return 'secondary_exposed' if any(flags.values()) else 'primary'


def independence(events):
    """Nearby/overlapping boundaries and shared utterances form one event group."""
    parent = list(range(len(events)))

    def root(i):
        while parent[i] != i:
            i = parent[i]
        return i

    for i, a in enumerate(events):
        for j, b in enumerate(events[:i]):
            near = (a['session_id'] == b['session_id'] and
                    a['session_start_ms'] < b['session_end_ms'] + 10_000 and
                    b['session_start_ms'] < a['session_end_ms'] + 10_000)
            shared = bool(set(a['utterance_ids']) & set(b['utterance_ids']))
            if near or shared or overlaps(a, b):
                parent[root(i)] = root(j)
    for i, e in enumerate(events):
        e['independence_group'] = anonymous(events[root(i)]['event_id'])
    return events


def build(state, output, legacy_db, prior_manifest=None):
    state, output = Path(state).resolve(), Path(output).resolve()
    if output.is_relative_to(state) or (output/'manifest.json').exists():
        raise ValueError('output must be new and outside runtime state')
    output.mkdir(parents=True, exist_ok=True)
    before = snapshot(state)
    write(output/'asset-fingerprints-before.json', before)
    frozen = assets(state)
    sources = frozen['metadata']['source_files']
    assert all(digest(s['path']) == s['sha256'] for s in sources), 'enrollment digest changed'
    c = readonly(state)
    self_id = c.execute("SELECT person_id FROM persons WHERE kind='self'").fetchone()[0]
    calibration = []
    calibration_files = {}
    for p in sorted((state/'identity').glob('identity-calibration-*.json')):
        data = read(p)
        calibration.extend(calibration_ranges(data, legacy_db))
        calibration_files[str(p)] = {'sha256': digest(p), 'samples': data['samples']}
    if not calibration:
        raise ValueError('cannot establish calibration source provenance')
    media_hashes = {r[0]: r[1] for r in c.execute('SELECT media_id,sha256 FROM audio_assets')}
    profiles = []
    for table, column in [('voice_prototypes', 'representative_clips_json'),
                          ('annotation_sample_sets', 'windows_json')]:
        for r in c.execute(f'SELECT {column} FROM {table}'):
            for clip in json.loads(r[0]):
                if clip.get('media_id') in media_hashes:
                    profiles.append({'sha256': media_hashes[clip['media_id']],
                                     'start_ms': clip['start_ms'], 'end_ms': clip['end_ms']})
    fitting = []
    if prior_manifest:
        for e in read(prior_manifest)['events']:
            if e.get('partition') == 'development' or e.get('split') == 'development':
                fitting.append({'sha256': e['sha256'], 'start_ms': e['source_start_ms'],
                                'end_ms': e['source_end_ms']})
    excluded = Counter()
    events = []
    checked_hashes = {}
    rows = c.execute('''SELECT s.*,e.* FROM speaker_purity_sources s
        JOIN speaker_purity_current cur USING(source_key)
        JOIN speaker_source_purity_evidence e USING(evidence_id) ORDER BY s.source_key''').fetchall()
    for r in rows:
        truth = strict_review(r, self_id)
        if truth is None:
            excluded['no_unambiguous_human_clean_review'] += 1
            continue
        if r['end_ms'] - r['start_ms'] < 1000:
            excluded['below_1s'] += 1
            continue
        captures = c.execute('''SELECT s.*,a.sha256,a.media_id,a.duration_ms,r.storage_key,
            x.captured_start,x.legacy_ref FROM capture_segments s
            JOIN audio_assets a USING(asset_id) JOIN audio_replicas r USING(replica_id)
            JOIN recording_sessions x USING(session_id)
            WHERE a.media_id=? AND r.state='available' AND x.tombstoned_at IS NULL''',
            (r['source_media_id'],)).fetchall()
        if not captures:
            excluded['missing_audio_mapping'] += 1
            continue
        frozen_session = False
        for cap in captures:
            sid = cap['session_id']
            role = c.execute('SELECT dataset_role FROM session_dataset_roles WHERE session_id=?', (sid,)).fetchone()
            reservation = c.execute('SELECT research_role FROM session_speaker_reservations WHERE session_id=?', (sid,)).fetchone()
            predicted = c.execute('SELECT 1 FROM blind_query_views q JOIN blind_prediction_snapshots p USING(query_id) WHERE q.session_id=? LIMIT 1', (sid,)).fetchone()
            if not role or role[0] != 'learning' or (reservation and reservation[0] != 'learning') or predicted:
                frozen_session = True
        if frozen_session:
            excluded['blind_holdout_independent_or_missing_role'] += 1
            continue
        cap = sorted(captures, key=lambda a: (a['session_id'], a['sequence']))[0]
        if r['start_ms'] < cap['source_start_ms'] or r['end_ms'] > cap['source_end_ms']:
            excluded['range_outside_capture'] += 1
            continue
        path = state/'audio'/cap['storage_key']
        if not path.is_file():
            excluded['missing_audio'] += 1
            continue
        if str(path) not in checked_hashes:
            checked_hashes[str(path)] = digest(path)
        sha = checked_hashes[str(path)]
        if sha != cap['sha256']:
            excluded['audio_sha_mismatch'] += 1
            continue
        offset = cap['session_start_ms'] - cap['source_start_ms']
        lo, hi = r['start_ms'] + offset, r['end_ms'] + offset
        uids = [u[0] for u in c.execute('''SELECT utterance_id FROM utterances
            WHERE session_id=? AND status='active' AND start_ms<? AND end_ms>?''',
            (cap['session_id'], hi, lo))]
        e = {'event_id': anonymous(r['source_key']), 'truth': truth,
             'ground_truth_source': 'individual_human_clean_single_review',
             'human_review_references': json.loads(r['review_references_json']),
             'review_provenance': json.loads(r['provenance_json']),
             'session_id': cap['session_id'], 'session': anonymous(cap['session_id']),
             'date': cap['captured_start'][:10], 'media_id': cap['media_id'],
             'storage_key': cap['storage_key'], 'sha256': sha,
             'start_ms': r['start_ms'], 'end_ms': r['end_ms'],
             'duration_ms': r['end_ms']-r['start_ms'],
             'session_start_ms': lo, 'session_end_ms': hi, 'utterance_ids': uids,
             'quality_flags': ['human_clean_single', 'no_conflicting_review'],
             'selection_bias': 'existing_purity_audit_targets_not_random_sample'}
        e['leakage'] = leak_flags(e, {s['sha256'] for s in sources}, calibration, profiles, fitting)
        e['cohort'] = cohort_for(e['leakage'])
        if e['cohort'] is None:
            excluded['enrollment_or_calibration'] += 1
            continue
        events.append(e)
    eligible_ranges = len(events)
    independence(events)
    # One longest boundary per independent group/truth; never choose by score.
    selected = {}
    for e in sorted(events, key=lambda x: (x['cohort'] != 'primary', -x['duration_ms'], x['event_id'])):
        selected.setdefault((e['truth'], e['independence_group']), e)
    events = sorted(selected.values(), key=lambda x: (x['truth'], x['date'], x['event_id']))
    by_truth = Counter()
    limited = []
    for e in events:
        if by_truth[e['truth']] < 100:
            limited.append(e)
            by_truth[e['truth']] += 1
    events = limited
    summary = {'counts': dict(Counter(f"{e['cohort']}:{e['truth']}" for e in events)),
               'excluded': dict(excluded), 'reviewed_source_ranges': len(rows),
               'eligible_ranges_before_independence': eligible_ranges,
               'deduplicated_event_count': len(events),
               'batch_annotations_are_not_strict_clean_truth': True}
    for cohort in ['primary', 'secondary_exposed']:
        for truth in ['self', 'non-self']:
            group = [e for e in events if e['cohort'] == cohort and e['truth'] == truth]
            summary[f'{cohort}:{truth}'] = {'events': len(group),
                'sessions': len({e['session'] for e in group}), 'dates': len({e['date'] for e in group}),
                'duration_ms': sum(e['duration_ms'] for e in group)}
    manifest = {'format': FORMAT, 'assets': frozen, 'events': events,
                'self_person_id': self_id, 'read_only': True, 'production_threshold_unchanged': True}
    write(output/'manifest.json', manifest)
    (output/'manifest.sha256').write_text(digest(output/'manifest.json'), encoding='ascii')
    write(output/'manifest-summary.json', summary)
    write(output/'leakage-audit.json', {'calibration_files': calibration_files,
          'calibration_source_ranges': calibration, 'profile_source_ranges': profiles,
          'previous_fitting_ranges': fitting, 'source_sha_checks': checked_hashes})
    c.close()
    return summary


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state-dir', type=Path, default=Path('state/v3'))
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--legacy-db', type=Path, required=True)
    p.add_argument('--prior-manifest', type=Path)
    args = p.parse_args()
    print(json.dumps(build(args.state_dir, args.output, args.legacy_db, args.prior_manifest)))
