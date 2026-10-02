"""Freeze the two prior failures and diagnose production application on isolated DBs."""
import argparse
import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

from short_self_dataset import digest, read, readonly, write
from allday_asr.v3.adapters.audio.tools import AudioClip, assemble_audio_clips
from allday_asr.v3.adapters.sqlite.blind_queries import _evidence, latest_run
from allday_asr.v3.adapters.sqlite.product_self_queries import product_self_queries


def intersect(turn, lo, hi):
    start, end = max(lo, turn['start_ms']), min(hi, turn['end_ms'])
    return {'start_ms': start, 'end_ms': end, 'duration_ms': end-start} if start < end else None


def prepare(state, previous, output):
    state, output = Path(state).resolve(), Path(output).resolve()
    if output.is_relative_to(state) or (output/'manifest.json').exists():
        raise ValueError('new immutable manifest required outside state')
    previous = Path(previous)
    manifest = read(previous/'manifest.json')
    pairs = read(previous/'production-paired-exact.json')
    c = readonly(state)
    cases = []
    for reason, label in [('not_contained_in_single_exclusive_turn', 'A'),
                          ('overlapping_or_foreign_regular_turn', 'B')]:
        selected = [p for p in pairs if p.get('truth') == 'self' and p.get('same_event_query_reason') == reason]
        if len(selected) != 1:
            raise ValueError('previous fixed failure changed')
        pair = selected[0]
        e = next(e for e in manifest['events'] if e['event_id'] == pair['event_id'])
        sid, lo, hi = e['session_id'], e['session_start_ms'], e['session_end_ms']
        run = latest_run(c, sid)
        u = dict(c.execute('''SELECT u.*,t.label FROM utterances u JOIN speaker_tracks t
            ON t.speaker_track_id=u.original_speaker_track_id
            WHERE u.run_id=? AND u.status='active' AND u.start_ms<? AND u.end_ms>?
            ORDER BY min(u.end_ms,?)-max(u.start_ms,?) DESC,u.utterance_id LIMIT 1''',
            (run, hi, lo, hi, lo)).fetchone())
        data = _evidence(c, run, state/'artifacts')
        diar = data['v3_diarization_evidence']
        context_lo, context_hi = lo-2000, max(hi, u['end_ms'])+2000
        captures = [dict(r) for r in c.execute('''SELECT s.*,a.sha256,a.duration_ms,a.media_id,r.storage_key
            FROM capture_segments s JOIN audio_assets a USING(asset_id)
            JOIN audio_replicas r USING(replica_id) WHERE s.session_id=?
            AND s.session_start_ms<? AND s.session_end_ms>? ORDER BY s.sequence''',
            (sid, context_hi, context_lo))]
        turns = {k:[t for t in diar[k] if intersect(t, context_lo, context_hi)]
                 for k in ['regular_turns','exclusive_turns']}
        overlaps = [{'labels':[a['speaker_label'],b['speaker_label']],
            'source':'persisted pyannote regular annotation', **intersect(a,b['start_ms'],b['end_ms'])}
            for i,a in enumerate(turns['regular_turns']) for b in turns['regular_turns'][i+1:]
            if a['speaker_label'] != b['speaker_label'] and intersect(a,b['start_ms'],b['end_ms'])]
        token_ranges = [{k:t[k] for k in ['start_ms','end_ms','ordinal','metadata','source_refs'] if k in t}
            for t in data['v3_asr_evidence']['primary_tokens'] if intersect(t,context_lo,context_hi)]
        revisions = {'session':dict(c.execute('SELECT * FROM recording_sessions WHERE session_id=?',(sid,)).fetchone()),
            'processing_runs':[dict(r) for r in c.execute('SELECT * FROM processing_runs WHERE session_id=? ORDER BY created_at',(sid,))],
            'artifacts':[dict(r) for r in c.execute("SELECT * FROM artifacts WHERE run_id=? AND kind IN ('v3_asr_evidence','v3_diarization_evidence','v3_transcript_evidence')",(run,))],
            'speaker_cluster_runs':[dict(r) for r in c.execute('SELECT cluster_run_id,producer,model,model_version,status,created_at FROM speaker_cluster_runs WHERE session_id=?',(sid,))],
            'artifact_revision_semantics':'immutable artifact_id/status/hash; no numeric revision column',
            'capture_revision_semantics':'immutable segments; no numeric revision column'}
        case = {'case':label,'anonymous_event_id':e['event_id'], 'session_id':sid,
            'utterance_id':u['utterance_id'], 'utterance':u, 'human_review':[lo,hi],
            'expected_truth':'self','pure_matcher_score':pair['pure_score'], 'pure_matcher_decision':'self',
            'production_decision':'unknown','production_rejection_reason':reason,
            'audio_source':e,'captures':captures,'turns':turns,'overlaps':overlaps,
            'foreign_intersections':[dict(t,intersection=intersect(t,lo,hi)) for t in turns['regular_turns']
                if t['speaker_label']!=u['label'] and intersect(t,lo,hi)],
            'token_ranges':token_ranges,'diarization_model':diar['model'],
            'revisions':revisions, 'previous_pair':pair}
        cases.append(case)
        for name,start,end in [('human-clean',lo,hi),('context',context_lo,context_hi)] + [
                (f'automatic-target-{i}',max(lo,t['start_ms']),min(hi,t['end_ms']))
                for i,t in enumerate(turns['exclusive_turns']) if t['speaker_label']==u['label'] and intersect(t,lo,hi)] + [
                (f'foreign-intersection-{i}',max(lo,t['start_ms']),min(hi,t['end_ms']))
                for i,t in enumerate(turns['regular_turns']) if t['speaker_label']!=u['label'] and intersect(t,lo,hi)]:
            clips = tuple(AudioClip(state/'audio'/cap['storage_key'],
                cap['source_start_ms']+max(start,cap['session_start_ms'])-cap['session_start_ms'],
                cap['source_start_ms']+min(end,cap['session_end_ms'])-cap['session_start_ms'])
                for cap in captures if cap['session_start_ms']<end and cap['session_end_ms']>start)
            assemble_audio_clips(clips,output/f'case-{label}'/f'{name}.wav')
        write(output/f'case-{label}'/'timeline.json',case)
    write(output/'manifest.json', {'format':'fixed-self-boundary-v1','previous_manifest_sha256':digest(previous/'manifest.json'),
        'previous_pair_sha256':digest(previous/'production-paired-exact.json'),'cases':cases})
    (output/'manifest.sha256').write_text(digest(output/'manifest.json'),encoding='ascii')
    c.close()
    return [{'case':c['case'],'human_review':c['human_review'],'foreign_intersections':c['foreign_intersections']} for c in cases]


def diagnose(state, output, phase):
    from allday_asr.v3.adapters.models.funasr import FunASRBackend
    from allday_asr.v3.adapters.files import ContentAddressedStore
    from allday_asr.v3.adapters.self_identity import CalibratedSelfIdentityMatcher
    from allday_asr.v3.adapters.speaker_embeddings import FunASRSpeakerEmbeddingProvider
    from allday_asr.v3.bootstrap.core import V3CorePaths, compose_v3_core
    from allday_asr.v3.config import CodexReminderSettings
    from allday_asr.v3.application.product_self_identity import infer_product_self
    import torch
    torch.set_num_threads(2)
    state,output=Path(state).resolve(),Path(output).resolve()
    if digest(output/'manifest.json') != (output/'manifest.sha256').read_text(encoding='ascii'):
        raise ValueError('fixed cases changed')
    target=output/f'{phase}-application.json'
    root=output/f'{phase}-isolated-state'
    if target.exists() or (root/'core.sqlite3').exists() or root.is_relative_to(state):
        raise ValueError('new isolated run required')
    root.mkdir(parents=True,exist_ok=True)
    source=readonly(state)
    clone=sqlite3.connect(root/'core.sqlite3')
    source.backup(clone)
    clone.row_factory=sqlite3.Row
    cases=read(output/'manifest.json')['cases']
    triggers=clone.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='utterances'").fetchall()
    for name, _ in triggers:
        clone.execute('DROP TRIGGER "'+name+'"')
    for case in cases:
        u=case['utterance']
        evidence=json.loads(u['evidence_json'])
        evidence.pop('person_annotation',None)
        # Counterfactual automatic inputs, not changes to human truth/corrections.
        clone.execute('UPDATE utterances SET speaker_track_id=original_speaker_track_id,evidence_json=?,start_ms=?,end_ms=? WHERE utterance_id=?',
            (json.dumps(evidence),*( [u['start_ms'],u['end_ms']] if phase=='runtime-original' else case['human_review']),case['utterance_id']))
    for _, ddl in triggers:
        clone.execute(ddl)
    clone.commit()
    query_start=time.perf_counter()
    queries=product_self_queries(clone,cases[0]['session_id'],state/'artifacts')
    latency=time.perf_counter()-query_start
    human_before=[tuple(r) for r in clone.execute('SELECT * FROM correction_operations')]
    annotations_before=[tuple(r) for r in clone.execute('SELECT * FROM annotation_facts')]
    clone.close()
    backend=FunASRBackend(device='cpu')
    provider=FunASRSpeakerEmbeddingProvider(ContentAddressedStore(state/'audio'),
        backend_factory=lambda:backend,temp_root=output/f'{phase}-clips')
    core=compose_v3_core(V3CorePaths(root,root/'core.sqlite3',state/'audio',state/'artifacts'),
        codex_settings=CodexReminderSettings(enabled=False),speaker_embedding_provider=provider,
        self_identity_matcher=CalibratedSelfIdentityMatcher(state))
    try:
        started=time.perf_counter()
        result=infer_product_self(core.people,cases[0]['session_id'])
        elapsed=time.perf_counter()-started
        with core.database.read() as c:
            policy=json.loads(c.execute('SELECT policy_json FROM speaker_cluster_runs WHERE cluster_run_id=?',
                                      (result['product_identity_run_id'],)).fetchone()[0])
            assert human_before==[tuple(r) for r in c.execute('SELECT * FROM correction_operations')]
            assert annotations_before==[tuple(r) for r in c.execute('SELECT * FROM annotation_facts')]
        ids={c['utterance_id']:c['case'] for c in cases}
        selected=[dict(t,case=ids[t['utterance_id']]) for t in policy['traces'] if t['utterance_id'] in ids]
        report={'application':'infer_product_self / real repository, query, provider, matcher, correction projection guard',
            'query_seconds':latency,'application_seconds':elapsed,'cases':selected,
            'queries':[{k:v for k,v in q.items() if k!='tracks'} | {'tracks':[asdict(t) for t in q['tracks']]}
                       for q in queries if q['utterance_id'] in ids],
            'range_scope': 'original production utterance' if phase=='runtime-original' else 'human-reviewed event boundary (diagnostic oracle only)',
            'human_corrections_and_annotation_facts_preserved':True,'writes':'isolated DB only'}
        write(target,report)
        return [{'case':t['case'],'decision':t['decision_before_projection'],'reason':t['reason']} for t in selected]
    finally:
        core.close()
        source.close()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state-dir',type=Path,default=Path('state/v3'))
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--previous',type=Path,default=Path('outputs/pure-self-matcher-audit-20261002'))
    p.add_argument('--phase',choices=['prepare','before','after','runtime','runtime-original'],required=True)
    a=p.parse_args()
    print(json.dumps(prepare(a.state_dir,a.previous,a.output) if a.phase=='prepare' else diagnose(a.state_dir,a.output,a.phase)))
