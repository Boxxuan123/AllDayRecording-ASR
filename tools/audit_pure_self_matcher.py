"""Read-only audio -> production preprocessing -> CAM++ -> standalone self matcher."""
import argparse
import csv
import hashlib
import json
import random
from dataclasses import asdict
from pathlib import Path

import numpy as np
import soundfile as sf

from build_pure_self_eval_manifest import FORMAT, anonymous
from historical_self_integrity import snapshot
from short_self_dataset import assets, digest, read, readonly, write
from allday_asr.v3.adapters.audio.tools import extract_clip
from allday_asr.v3.adapters.models.funasr import FunASRBackend
from allday_asr.v3.adapters.self_identity import CalibratedSelfIdentityMatcher
from allday_asr.v3.domain.people import SpeakerEmbedding

BINS = ('1-2s', '2-3s', '3-4s', '4-6s', '6s+')


def bucket(ms):
    return BINS[0] if ms < 2000 else BINS[1] if ms < 3000 else BINS[2] if ms < 4000 else BINS[3] if ms < 6000 else BINS[4]


def crop_ranges(start, end):
    """Centered duration probes are correlated samples, never extra events."""
    if start < 0 or end-start < 1000:
        raise ValueError('invalid human boundary')
    result = [('full', start, end)]
    for length in (1500, 2000, 3000, 4000, 6000):
        if length < end-start:
            lo = start + (end-start-length)//2
            result.append((f'crop-{length}', lo, lo+length))
    return result


def decision(matcher, raw, ms):
    vector = np.asarray(raw, dtype=np.float32).copy()
    norm = np.linalg.norm(vector)
    if not np.isfinite(vector).all() or norm <= 0:
        raise ValueError('invalid CAM++ embedding')
    vector /= norm
    embedding = SpeakerEmbedding('pure-audit', 'FunASR/CAM++', 'v1-local',
        tuple(float(x) for x in vector), (), min(1.0, ms/12000))
    return matcher.match(embedding).evidence, vector


def pure_infer(waves, durations, backend, matcher):
    """No repository, named-person matcher, ownership or identity write here."""
    raw = backend.extract_speaker_embeddings(waves, batch_size=8)
    if len(raw) != len(waves):
        raise ValueError('embedding count mismatch')
    return [decision(matcher, r, ms)[0] for r, ms in zip(raw, durations, strict=True)]


def stats(values):
    if not values:
        return {'n': 0, 'min': None, 'p25': None, 'median': None, 'p75': None, 'max': None}
    a = np.asarray(values)
    return {'n': len(a), 'min': float(a.min()), 'p25': float(np.quantile(a, .25)),
            'median': float(np.median(a)), 'p75': float(np.quantile(a, .75)), 'max': float(a.max())}


def metrics(rows):
    def count(truth, accepted):
        return sum(r['truth'] == truth and (r['decision'] == 'self') == accepted for r in rows)
    tp, fn, fp, tn = count('self', True), count('self', False), count('non-self', True), count('non-self', False)
    return {'TP': tp, 'FN': fn, 'FP': fp, 'TN': tn,
            'self_recognition_rate': tp/(tp+fn) if tp+fn else None,
            'self_scores': stats([r['score'] for r in rows if r['truth'] == 'self']),
            'non_self_scores': stats([r['score'] for r in rows if r['truth'] == 'non-self']),
            'diagnostic_only': True}


def invariance(waves, durations, backend, matcher, indices):
    short, long = min(waves, key=len)[:16000], max(waves, key=len)
    rows = []
    for index in indices:
        target = waves[index]
        base = None
        for context, inputs, pos in [('single', [target], 0), ('repeat', [target], 0),
              ('short_companion', [target, short], 0), ('long_companion', [target, long], 0),
              ('reverse_order', [long, short, target], 2)]:
            raw = backend.extract_speaker_embeddings(inputs, batch_size=len(inputs))[pos]
            evidence, vector = decision(matcher, raw, durations[index])
            if base is None:
                base = (vector, evidence)
            rows.append({'sample_index': index, 'context': context,
                'embedding_drift': float(np.max(np.abs(vector-base[0]))),
                'score_drift': abs(evidence['score']-base[1]['score']),
                'decision_flip': evidence['decision'] != base[1]['decision'],
                'score': evidence['score'], 'decision': evidence['decision']})
    return {'targets': len(indices), 'max_embedding_drift': max(r['embedding_drift'] for r in rows),
            'max_score_drift': max(r['score_drift'] for r in rows),
            'decision_flips': sum(r['decision_flip'] for r in rows), 'rows': rows}


def paired(state, events, full_rows, backend, matcher, output):
    """Secondary counterfactual only; production SQLite stays read-only.

    Restore pre-annotation track metadata on an in-memory backup to measure the
    automatic query. Do not change event bounds or automatic acoustic evidence.
    Human-reviewed subranges and original utterances may differ; report both.
    """
    import sqlite3
    from allday_asr.v3.adapters.sqlite.product_self_queries import product_self_queries
    from allday_asr.v3.adapters.sqlite.blind_queries import latest_run
    from allday_asr.v3.adapters.files import ContentAddressedStore
    from allday_asr.v3.adapters.speaker_embeddings import FunASRSpeakerEmbeddingProvider
    from allday_asr.v3.domain.product_self_gate import product_self_gate

    source = readonly(state)
    clone = sqlite3.connect(':memory:')
    clone.row_factory = sqlite3.Row
    source.backup(clone)
    for (name,) in clone.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
        clone.execute('DROP TRIGGER "'+name+'"')
    provider = FunASRSpeakerEmbeddingProvider(ContentAddressedStore(state/'audio'),
        backend_factory=lambda: backend, temp_root=output/'paired-clips')
    result = []
    pure = {r['event_id']: r for r in full_rows}
    for e in events:
        run = latest_run(source, e['session_id'])
        u = source.execute('''SELECT * FROM utterances WHERE run_id=? AND status='active'
            AND start_ms<? AND end_ms>? ORDER BY
            min(end_ms,?)-max(start_ms,?) DESC,utterance_id LIMIT 1''',
            (run, e['session_end_ms'], e['session_start_ms'], e['session_end_ms'], e['session_start_ms'])).fetchone()
        if u is None:
            result.append({'event_id': e['event_id'], 'paired_available': False})
            continue
        actual = next((q for q in product_self_queries(source, e['session_id'], state/'artifacts')
                       if q['utterance_id'] == u['utterance_id']), None)
        evidence = json.loads(u['evidence_json'])
        evidence.pop('person_annotation', None)
        clone.execute('UPDATE utterances SET speaker_track_id=original_speaker_track_id,evidence_json=? WHERE utterance_id=?',
                      (json.dumps(evidence), u['utterance_id']))
        query = next((q for q in product_self_queries(clone, e['session_id'], state/'artifacts')
                      if q['utterance_id'] == u['utterance_id']), None)
        windows = []
        if query:
            for t, embedding in zip(query['tracks'], provider.embed(query['tracks']), strict=True):
                windows.append({'input': asdict(t), 'duration_ms': sum(c.source_end_ms-c.source_start_ms for c in t.clips),
                                **matcher.match(embedding).evidence})
        identity, reason, _ = product_self_gate(windows, query['reason'] if query else 'query_unavailable')
        # A second diagnostic gives the builder precisely the human event
        # boundary. This is not an edit to the stored utterance or production
        # builder; it prevents a longer utterance being called the same audio.
        clone.execute('UPDATE utterances SET start_ms=?,end_ms=? WHERE utterance_id=?',
                      (e['session_start_ms'], e['session_end_ms'], u['utterance_id']))
        exact = next((q for q in product_self_queries(clone, e['session_id'], state/'artifacts')
                      if q['utterance_id'] == u['utterance_id']), None)
        exact_windows = []
        if exact:
            for t, embedding in zip(exact['tracks'], provider.embed(exact['tracks']), strict=True):
                exact_windows.append({'input': asdict(t), 'duration_ms': sum(c.source_end_ms-c.source_start_ms for c in t.clips),
                                      **matcher.match(embedding).evidence})
        exact_identity, exact_reason, _ = product_self_gate(exact_windows, exact['reason'] if exact else 'query_unavailable')
        result.append({'event_id': e['event_id'], 'truth': e['truth'], 'paired_available': True,
            'pure_score': pure[e['event_id']]['score'], 'pure_decision': pure[e['event_id']]['decision'],
            'actual_product_reason': actual['reason'] if actual else 'not_current',
            'counterfactual_product_decision': identity.value, 'counterfactual_product_reason': reason,
            'manual_metadata_restored_in_memory': True, 'windows': windows,
            'same_event_query_decision': exact_identity.value,
            'same_event_query_reason': exact_reason, 'same_event_windows': exact_windows,
            'human_reviewed_range': [e['session_start_ms'], e['session_end_ms']],
            'production_utterance_range': [u['start_ms'], u['end_ms']],
            'exact_same_range': [e['session_start_ms'], e['session_end_ms']] == [u['start_ms'], u['end_ms']]})
        clone.execute('UPDATE utterances SET speaker_track_id=?,evidence_json=?,start_ms=?,end_ms=? WHERE utterance_id=?',
                      (u['speaker_track_id'], u['evidence_json'], u['start_ms'], u['end_ms'], u['utterance_id']))
    source.close()
    clone.close()
    return result


def run(state, output):
    state, output = Path(state).resolve(), Path(output).resolve()
    if output.is_relative_to(state) or (output/'scores.json').exists():
        raise ValueError('new output required outside runtime state')
    manifest = read(output/'manifest.json')
    if manifest['format'] != FORMAT or digest(output/'manifest.json') != (output/'manifest.sha256').read_text(encoding='ascii'):
        raise ValueError('frozen manifest changed')
    if assets(state) != manifest['assets']:
        raise ValueError('frozen assets changed')
    import torch
    torch.set_num_threads(2)
    matcher, backend = CalibratedSelfIdentityMatcher(state), FunASRBackend(device='cpu')
    events = manifest['events']
    records, waves = [], []
    for e in events:
        if digest(state/'audio'/e['storage_key']) != e['sha256']:
            raise ValueError('audio content changed')
        for kind, lo, hi in crop_ranges(e['start_ms'], e['end_ms']):
            destination = output/'clips'/f"{e['event_id']}-{kind}.wav"
            extract_clip(state/'audio'/e['storage_key'], destination, lo, hi)
            wave, rate = sf.read(destination, dtype='float32')
            if rate != 16000 or wave.ndim != 1 or abs(len(wave)/16-(hi-lo)) > 2:
                raise ValueError('normalization or boundary mismatch')
            waves.append(wave)
            records.append({k: e[k] for k in ['event_id', 'truth', 'session', 'date', 'cohort', 'independence_group', 'quality_flags']} |
                {'sample_kind': kind, 'duration_ms': hi-lo, 'bucket': bucket(hi-lo),
                 'normalized_wave_sha256': hashlib.sha256(wave.tobytes()).hexdigest(),
                 'audio_rms': float(np.sqrt(np.mean(wave**2))),
                 'clipping_fraction': float(np.mean(np.abs(wave) >= .999)),
                 'model_version': 'FunASR/CAM++/1.4.4/single-waveform-v2',
                 'voiceprint_sha256': matcher.status()['voiceprint_sha256']})
    evidence = pure_infer(waves, [r['duration_ms'] for r in records], backend, matcher)
    for row, d in zip(records, evidence, strict=True):
        row.update(d)
        row['threshold'] = d['self_threshold']
        row['margin_from_threshold'] = d['score']-d['self_threshold']
    write(output/'scores.json', records)
    scalar_keys = ['event_id', 'truth', 'session', 'date', 'cohort', 'sample_kind', 'duration_ms', 'bucket',
                   'score', 'threshold', 'decision', 'reason', 'margin_from_threshold', 'model_version', 'voiceprint_sha256']
    with (output/'scores.csv').open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, scalar_keys, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(records)
    distribution = []
    for cohort in ['primary', 'secondary_exposed']:
        for scope in ['independent_full_events', 'correlated_duration_crops']:
            for bin_name in BINS:
                rows = [r for r in records if r['cohort'] == cohort and r['bucket'] == bin_name and
                        (r['sample_kind'] == 'full') == (scope == 'independent_full_events')]
                distribution.append({'cohort': cohort, 'scope': scope, 'bucket': bin_name, **metrics(rows)})
    write(output/'duration-buckets.json', distribution)
    write(output/'score-distribution.json', distribution)
    with (output/'score-distribution.csv').open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, ['cohort', 'scope', 'bucket', 'truth', 'n', 'min', 'p25', 'median', 'p75', 'max'])
        writer.writeheader()
        for d in distribution:
            for truth, key in [('self', 'self_scores'), ('non-self', 'non_self_scores')]:
                writer.writerow({k:d[k] for k in ['cohort', 'scope', 'bucket']} | {'truth': truth} | d[key])
    full_indices = [i for i,r in enumerate(records) if r['sample_kind'] == 'full']
    chosen = random.Random(20261002).sample(full_indices, min(20, len(full_indices)))
    cpu = invariance(waves, [r['duration_ms'] for r in records], backend, matcher, chosen)
    batch = {'cpu': cpu, 'cuda_available': torch.cuda.is_available(), 'seed': 20261002,
             'adapter_model_call_batch_size': 1}
    if torch.cuda.is_available():
        cuda = invariance(waves, [r['duration_ms'] for r in records], FunASRBackend(device='cuda:0'), matcher, chosen)
        batch['cuda'] = cuda
        batch['cpu_cuda_max_score_difference'] = max(abs(a['score']-b['score']) for a,b in zip(cpu['rows'],cuda['rows'],strict=True))
        batch['cpu_cuda_decision_flips'] = sum(a['decision']!=b['decision'] for a,b in zip(cpu['rows'],cuda['rows'],strict=True))
    batch['engineering_valid'] = all(batch[k]['decision_flips'] == 0 and batch[k]['max_score_drift'] <= 1e-5
                                     and batch[k]['max_embedding_drift'] <= 1e-5 for k in ['cpu', 'cuda'] if k in batch)
    write(output/'batch-invariance.json', batch)
    # No enrollment originals are part of the evaluation or invariance corpus.
    sanity = []
    for i,s in enumerate(manifest['assets']['metadata']['source_files']):
        clip = output/'sanity'/f'{i}.wav'
        ms = min(8000, int(s['duration_seconds']*1000))
        extract_clip(Path(s['path']), clip, 0, ms)
        wave, rate = sf.read(clip, dtype='float32')
        assert rate == 16000
        d = pure_infer([wave], [ms], backend, matcher)[0]
        sanity.append({'source': anonymous(s['sha256']), 'scope': 'SANITY_ONLY', 'duration_ms': ms, **d})
    write(output/'enrollment-sanity.json', sanity)
    full = [r for r in records if r['sample_kind'] == 'full']
    pairs = paired(state, events, full, backend, matcher, output)
    write(output/'production-paired.json', pairs)
    failures = []
    by_pair = {p['event_id']:p for p in pairs}
    for r in full:
        if r['truth'] != 'self':
            continue
        p = by_pair[r['event_id']]
        reason = p.get('same_event_query_reason')
        query_categories = {'not_contained_in_single_exclusive_turn': 'QUERY_OWNERSHIP',
            'overlapping_or_foreign_regular_turn': 'QUERY_FOREIGN_SPEAKER_OR_OVERLAP',
            'insufficient_clean_windows': 'QUERY_TOO_SHORT',
            'automatic_turn_evidence_unavailable:ValueError': 'QUERY_MISSING_EVIDENCE',
            'incomplete_or_mixed_query': 'QUERY_MAPPING'}
        category = 'MATCHER_FALSE_REJECT' if r['decision'] != 'self' else (
            'PASS' if p.get('same_event_query_decision') == 'self' else query_categories.get(reason, 'OTHER'))
        failures.append({'event_id': r['event_id'], 'category': category,
            'query_reason': reason, 'stored_utterance_matches_human_boundary': p.get('exact_same_range')})
    write(output/'failure-taxonomy.json', failures)
    after = snapshot(state)
    before = read(output/'asset-fingerprints-before.json')
    stable = [k for k in before['all_tables'] if k not in {'audit_entries', 'device_credentials', 'sync_cursors'}]
    integrity = {'assets_unchanged': before['assets'] == after['assets'],
                 'protected_unchanged': before['protected_tables'] == after['protected_tables'],
                 'production_tables_unchanged': all(before['all_tables'][k] == after['all_tables'][k] for k in stable),
                 'background_only_exceptions': ['audit_entries', 'device_credentials', 'sync_cursors']}
    write(output/'asset-fingerprints-after.json', after)
    write(output/'integrity.json', integrity)
    assert all(integrity[k] for k in ['assets_unchanged', 'protected_unchanged', 'production_tables_unchanged'])
    summary = {'full': {c: metrics([r for r in full if r['cohort']==c]) for c in ['primary','secondary_exposed']},
               'engineering_valid': batch['engineering_valid'], 'integrity': integrity}
    write(output/'results-summary.json', summary)
    return summary


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state-dir', type=Path, default=Path('state/v3'))
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(run(a.state_dir, a.output)))
