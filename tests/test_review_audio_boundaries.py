"""Independent user-facing playback probes; no production modules modified.

Uses a real test SQLite core, real content store and real FFmpeg for both embedding
input crops and review playback. Only the numerical speaker-model backend is
replaced by fixed unit vectors; these tests say nothing about identification accuracy.
"""
import base64
import io
import json
import os
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from tests.test_phase1_human_facts import people as people
from tests.test_phase2_samples import short_rows, save
from allday_asr.v3.adapters.speaker_embeddings import FunASRSpeakerEmbeddingProvider
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
from allday_asr.v3.interfaces.device_reviews import DeviceReviewService


@pytest.fixture
def actual_audio(people):
    f = people
    sr = 16000
    times = np.arange(10 * sr) / sr
    # Distinct frequencies let a later implementation verify which ranges played.
    freq = np.where(times < 3, 220, np.where(times < 6.5, 440, 880))
    audio = (0.2 * np.sin(2 * np.pi * freq * times)).astype(np.float32)
    buf = io.BytesIO()
    sf.write(buf, audio, sr, format='WAV', subtype='PCM_16')
    stored = f.core.audio_store.put_bytes(buf.getvalue())
    with SqliteUnitOfWork(f.core.database) as u:
        u.people.connection.execute('UPDATE audio_replicas SET storage_key=? WHERE asset_id=?', (stored.storage_key, 'asset-1'))
        # Existing fixture media metadata is synthetic and immutable. Only bind
        # its replica to a real synthetic WAV stored through the production store.
    seen = []
    class Backend:
        def extract_speaker_embeddings(self, samples, *, batch_size=8):
            seen.append([len(s) for s in samples])
            return np.tile(np.array([1., 0., 0.], dtype=np.float32), (len(samples), 1))
    provider = FunASRSpeakerEmbeddingProvider(f.core.audio_store, backend_factory=Backend, temp_root=f.root/'independent-clips')
    f.core.people._provider = provider
    return f, seen, provider


def pending_sample(f, ids):
    pid = f.core.people.create_person('Synthetic A')['person_id']
    save(f, ids, pid)
    jobs = f.core.people.sample_worker.run_pending()
    assert jobs and jobs[-1]['status'] == 'processed', jobs
    candidate = f.core.people.list_review_candidates(pid)[0]
    return pid, candidate


def test_advertised_historical_grant_audio_is_playable(actual_audio):
    f, seen, provider = actual_audio
    ids = short_rows(f)
    pid, candidate = pending_sample(f, ids[:2])
    prototype = candidate['prototype_id']
    f.core.people.review_prototype(prototype, pid, 'confirmed')
    service = DeviceReviewService(f.core)
    item = next(i for i in service.snapshot()['items'] if i['context'].get('voice_mode') == 'accepted_grant')
    assert item['context']['voice_candidates'][0]['representative_clips']
    dump = os.environ.get('INDEPENDENT_AUDIO_PROBE_DIR')
    if dump:
        Path(dump, 'accepted-grant-item.json').write_text(json.dumps(item, ensure_ascii=False))
    print('GRANT_CANDIDATE_HAS_REAL_SOURCE', f.core.audio_store.path_for(f.core.desktop.media('media-1')['storage_key']).exists())
    print('GRANT_UI_CONTEXT', item['context']['voice_mode'])
    result = service.audio({'review_id': item['review_id'], 'prototype_id': prototype})
    assert base64.urlsafe_b64decode(result['data_base64url'] + '==').startswith(b'RIFF')


@pytest.mark.parametrize('windows', [
    ((0,3000),(3500,6500),(7000,10000)),
    ((0,800),(900,1700),(1800,2600),(2700,3500),(3600,4400),(4500,10000)),
])
def test_default_aggregate_audition_covers_selected_evidence(actual_audio, windows):
    f, seen, provider = actual_audio
    ids = short_rows(f, windows)
    pid, candidate = pending_sample(f, ids)
    service = DeviceReviewService(f.core)
    prototype = candidate['prototype_id']
    item = next(i for i in service.snapshot()['items'] if i['context'].get('voice_mode') == 'known_person' and prototype in i['context'].get('prototype_ids', []))
    result = service.audio({'review_id': item['review_id'], 'prototype_id': prototype})
    wav = base64.urlsafe_b64decode(result['data_base64url'] + '==')
    samples, sr = sf.read(io.BytesIO(wav), dtype='float32')
    selected_ms = sum(c['end_ms']-c['start_ms'] for c in candidate['representative_clips'])
    observed_ms = len(samples)*1000/sr
    print('AUDITION_COVERAGE', json.dumps({'selected_ms': selected_ms, 'played_ms': observed_ms, 'provider_samples': seen[-1], 'windows':candidate['representative_clips']}))
    dump = os.environ.get('INDEPENDENT_AUDIO_PROBE_DIR')
    if dump:
        Path(dump, f'pending-{selected_ms}-item.json').write_text(json.dumps(item, ensure_ascii=False))
    # Confirming the prototype authorizes its full aggregate, not the previewed first clip.
    service.resolve('device-1', {'review_id': item['review_id'], 'prototype_id': prototype, 'action':'confirm'})
    with SqliteUnitOfWork(f.core.database) as u:
        assert u.people.person_vectors(provider.model, provider.model_version)
        accepted = u.people.connection.execute("SELECT representative_clips_json FROM voice_prototypes WHERE status='accepted'").fetchone()
        print('ACCEPTED_WINDOW_COUNT', len(json.loads(accepted[0])))
    # Proposed playback-consistency acceptance criterion, not an old P2 audit assertion.
    assert observed_ms == pytest.approx(selected_ms, abs=1), 'Default playback exposes only part of the aggregate that is accepted'
