"""Read-only self audit. Private IDs and audio ranges belong in an ignored manifest.

Example: python tools/audit_self_identity_regression.py --state-dir state/v3
  --manifest outputs/private/manifest.json --output outputs/private/audit.json
No enrollment, policy changes, production corrections, or Blind rescoring occur.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import numpy as np

from allday_asr.v3.adapters.files import ContentAddressedStore
from allday_asr.v3.adapters.models.funasr import FunASRBackend, _cached_model_or_id
from allday_asr.v3.adapters.self_identity import CalibratedSelfIdentityMatcher
from allday_asr.v3.adapters.speaker_embeddings import FunASRSpeakerEmbeddingProvider
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork, V3Database
from allday_asr.v3.domain.people import cosine_similarity
from allday_asr.v3.ports.speaker_embeddings import SpeakerClipInput, SpeakerTrackInput


def fingerprints(connection):
    tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
              if r[0].startswith(('blind_', 'purity_', 'speaker_purity_')) or r[0] in (
                  'voice_prototypes', 'voice_prototype_reviews', 'annotation_facts',
                  'annotation_fact_audio', 'annotation_sample_sets', 'annotation_sample_revocations',
                  'person_identity_policy_revisions', 'session_dataset_roles',
                  'session_learning_exposure', 'annotation_sample_queue')]
    result = {}
    for name in tables:
        rows = sorted([tuple(r) for r in connection.execute('SELECT * FROM '+name)], key=str)
        result[name] = {'rows': len(rows), 'sha256': hashlib.sha256(
            json.dumps(rows, default=str).encode()).hexdigest()}
    return result


def audit(state_dir, manifest, output, *, device='cpu'):
    state_dir, output = state_dir.resolve(), output.resolve()
    # A diagnostics command must not overwrite a policy, database, or source asset.
    if output.suffix != '.json' or output.is_relative_to(state_dir):
        raise ValueError('audit output must be a JSON file outside the runtime state directory')
    output.parent.mkdir(parents=True, exist_ok=True)
    events = json.loads(manifest.read_text(encoding='utf-8'))['events']
    matcher = CalibratedSelfIdentityMatcher(state_dir)
    backend = FunASRBackend(device=device)
    provider = FunASRSpeakerEmbeddingProvider(ContentAddressedStore(state_dir/'audio'),
        backend_factory=lambda: backend, temp_root=output.parent/'audit-clips')
    db = V3Database(state_dir/'core.sqlite3')
    tracks = []
    for i, event in enumerate(events):
        clips = event.get('production_clips') or [{k: event[k] for k in (
            'media_id', 'storage_key', 'source_start_ms', 'source_end_ms', 'utterance_id')}]
        tracks.append(SpeakerTrackInput(str(i), event['session_id'],
                                       tuple(SpeakerClipInput(**clip) for clip in clips)))
    diagnostic = {e.speaker_track_id: e for e in provider.embed(tuple(tracks))}
    traces = []
    with SqliteUnitOfWork(db).reading() as uow:
        c = uow.people.connection
        integrity = fingerprints(c)
        people = uow.people.person_vectors(provider.model, provider.model_version)
        status = matcher.status()
        self_id = uow.people.self_person_id()
        assets = {'self_person_id': self_id, 'standalone_anchor': status,
                  'database_self_prototype_count': c.execute(
                      'SELECT COUNT(*) FROM voice_prototypes WHERE person_id=?', (self_id,)).fetchone()[0]}
        if status.get('available'):
            policy = json.loads((state_dir/'identity/active-self-identity-policy.json').read_text(encoding='utf-8'))
            assets['active_policy'] = policy
            with np.load(policy['voiceprint'], allow_pickle=False) as payload:
                assets['embedding_dimensions'] = int(payload['embeddings'].shape[1])
                if 'metadata_json' in payload:
                    metadata = json.loads(str(payload['metadata_json']))
                    assets['enrollment_metadata'] = {k: metadata.get(k) for k in (
                        'speaker_model', 'speaker_model_version', 'created_at', 'source_files')}
        for i, event in enumerate(events):
            row = c.execute('SELECT * FROM utterances WHERE utterance_id=?',
                            (event['utterance_id'],)).fetchone()
            role = c.execute('SELECT dataset_role FROM session_dataset_roles WHERE session_id=?',
                             (event['session_id'],)).fetchone()
            product = None
            for run in c.execute("SELECT policy_json FROM speaker_cluster_runs WHERE session_id=? "
                                 "AND producer='product-self-inference' ORDER BY created_at DESC",
                                 (event['session_id'],)):
                product = next((t for t in json.loads(run[0])['traces']
                                if t['utterance_id']==event['utterance_id']), None)
                if product is not None:
                    break
            embedding = diagnostic.get(str(i))
            scores = [] if embedding is None else sorted([
                {'person_id': pid, 'similarity': cosine_similarity(embedding.vector, vector)}
                for pid, vector in people if len(vector)==len(embedding.vector)],
                key=lambda x: x['similarity'], reverse=True)
            traces.append({**event, 'session_dataset_role': role[0] if role else None,
                'actual_input': asdict(tracks[i]), 'self_enrollment': status,
                'model': provider.model, 'model_version': provider.model_version,
                'diagnostic_query_score': matcher.match(embedding).evidence if embedding else
                    {'decision': 'unknown', 'reason': 'embedding_unavailable'},
                'diagnostic_top_other_candidates': scores[:5],
                'diagnostic_other_margin': scores[0]['similarity']-scores[1]['similarity']
                    if len(scores)>1 else None,
                'product_trace': product or {'self_matcher_invoked': 'NOT RECORDED',
                    'reason': 'non-learning early return' if role and role[0]!='learning' else
                              'historical self matcher evidence not retained'},
                'final_pc_projection': row['identity'] if row else 'MISSING',
                'final_phone_visible_identity': 'NOT VERIFIED: device state unavailable'})
    model_root = Path(_cached_model_or_id('cam++'))
    model_files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in model_root.iterdir() if p.is_file() and p.suffix in ('.bin','.json','.yaml')}
    report = {'purpose': 'private diagnostic; not a Blind score', 'events': traces, 'self_assets': assets,
              'model_files': model_files, 'fingerprints': integrity,
              'projection_counts': {f'{truth}:{identity}': count for (truth,identity),count in
                  Counter((t['truth'],t['final_pc_projection']) for t in traces).items()}}
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--device', default='cpu')
    args = parser.parse_args()
    report = audit(args.state_dir, args.manifest, args.output, device=args.device)
    print(json.dumps({'events': len(report['events']), 'projection_counts': report['projection_counts']}))


if __name__ == '__main__':
    main()
