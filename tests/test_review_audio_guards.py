"""Synthetic WAVs only. FFmpeg, SQLite, source eligibility and grants are real."""
import base64
import io
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from tests.test_review_audio_boundaries import actual_audio as actual_audio, people as people, pending_sample
from tests.test_phase2_samples import short_rows, save, classify
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
from allday_asr.v3.interfaces.device_reviews import DeviceReviewService, _normalized_review_audio
from allday_asr.v3.interfaces.review_audio import audio_plan
from allday_asr.v3.interfaces.transfer.devices import DeviceConflictError


def decode(response):
    return sf.read(io.BytesIO(base64.urlsafe_b64decode(response['data_base64url'] + '==')), dtype='int16')


def test_exact_waveform_order_and_gaps(actual_audio):
    f, seen, provider = actual_audio
    pid, c = pending_sample(f, short_rows(f))
    service = DeviceReviewService(f.core)
    response = service.desktop_audio(dict(prototype_id=c['prototype_id'], person_id=pid))
    actual, rate = decode(response)
    source = f.core.audio_store.path_for(f.core.desktop.media('media-1')['storage_key'])
    expected = []
    offset = 0
    for window, selected in zip(response['windows'], c['representative_clips'], strict=True):
        assert {k: window[k] for k in ('media_id', 'start_ms', 'end_ms')} == {k: selected[k] for k in ('media_id', 'start_ms', 'end_ms')}
        assert window['playback_start_ms'] == offset
        offset += window['end_ms'] - window['start_ms']
        part = _normalized_review_audio(source, window['start_ms'], window['end_ms'], temp_root=f.root/'reference')
        samples, part_rate = sf.read(io.BytesIO(part), dtype='int16')
        assert part_rate == rate
        expected.append(samples)
    np.testing.assert_array_equal(actual, np.concatenate(expected))
    assert seen[-1] == [48000, 48000, 48000]
    assert len(actual) / rate == 9
    assert not list((f.root/'reference').glob('*.wav'))
    assert not list((f.core.audio_store.root.parent/'review-audio-temp').glob('*.wav'))


@pytest.mark.parametrize('clips', [
    [], [dict(media_id='m', start_ms=0, end_ms=0)],
    [dict(media_id='m', start_ms=-1, end_ms=2)],
    [dict(media_id='m', start_ms=0, end_ms=3000)] * 2,
    [dict(media_id='m', start_ms=0, end_ms=3000), dict(media_id='m', start_ms=2000, end_ms=4000)],
    [dict(media_id=str(i), start_ms=0, end_ms=1000) for i in range(6)],
    [dict(media_id='m', start_ms=0, end_ms=40001)],
])
def test_invalid_or_overlapping_windows_rejected(clips):
    with pytest.raises(ValueError):
        audio_plan(dict(prototype_id='p', representative_clips=clips))


def test_cross_media_single_window_and_40_second_limit(tmp_path):
    paths = {}
    for name, frequency in [('m1', 220), ('m2', 880)]:
        paths[name] = tmp_path / (name + '.wav')
        time = np.arange(21*16000)/16000
        sf.write(paths[name], .2*np.sin(2*np.pi*frequency*time), 16000, subtype='PCM_16')
    core = SimpleNamespace(desktop=SimpleNamespace(media=lambda name: dict(storage_key=name)),
        audio_store=SimpleNamespace(root=tmp_path/'store', path_for=lambda key: paths[key]))
    service = DeviceReviewService(core)
    candidate = dict(prototype_id='p', representative_clips=[dict(media_id='m2', start_ms=1000, end_ms=21000), dict(media_id='m1', start_ms=0, end_ms=20000)])
    result = service.render_audio('r', candidate)
    samples, rate = decode(result)
    assert len(samples)/rate == 40
    assert [w['media_id'] for w in result['windows']] == ['m2', 'm1']
    # Each half has its own frequency; neither ordering nor inter-media gaps change.
    for start, frequency in [(0, 880), (20*rate, 220)]:
        spectrum = np.abs(np.fft.rfft(samples[start:start+rate]))
        assert np.argmax(spectrum) == frequency
    candidate['representative_clips'] = [dict(media_id='m1', start_ms=500, end_ms=1300)]
    assert len(decode(service.render_audio('r', candidate))[0]) == 12800
    assert not list((tmp_path/'review-audio-temp').glob('*.wav'))


def test_history_noncurrent_relisten_missing_file_and_independent_withdrawal(actual_audio):
    f, seen, provider = actual_audio
    ids = short_rows(f)
    pid, first = pending_sample(f, ids[:2])
    f.core.people.review_prototype(first['prototype_id'], pid, 'confirmed')
    save(f, ids[2:], pid)
    f.core.people.sample_worker.run_pending()
    second = f.core.people.list_review_candidates(pid)[0]
    f.core.people.review_prototype(second['prototype_id'], pid, 'confirmed')
    service = DeviceReviewService(f.core)
    review = f"voice-grant:{first['prototype_id']}:{pid}"
    request = dict(review_id=review, prototype_id=first['prototype_id'])
    assert len(decode(service.audio(request))[0]) == 96000
    with pytest.raises(DeviceConflictError):
        service.audio({**request, 'prototype_id': second['prototype_id']})
    with pytest.raises(DeviceConflictError):
        service.desktop_audio(dict(prototype_id=first['prototype_id'], person_id=f.core.people.create_person('Synthetic Other')['person_id']))
    with pytest.raises(ValueError):
        service.resolve('test', {**request, 'action': 'confirm'})
    def stable_rows():
        with SqliteUnitOfWork(f.core.database) as u:
            return [tuple(r) for r in u.people.connection.execute('SELECT * FROM utterances ORDER BY utterance_id')]
    facts = stable_rows()
    source = f.core.audio_store.path_for(f.core.desktop.media('media-1')['storage_key'])
    source.unlink()  # This fixture's synthetic file only.
    item = next(i for i in service.snapshot()['items'] if i['review_id'] == review)
    c = item['context']['voice_candidates'][0]
    assert c['audio_available'] is False and c['audio_unavailable_reason']
    with pytest.raises(FileNotFoundError):
        service.audio(request)
    service.resolve('test', {**request, 'action': 'retract'})
    assert stable_rows() == facts
    confirmed = f.core.people.list_review_candidates(pid, 'confirmed')
    assert [c['prototype_id'] for c in confirmed] == [second['prototype_id']]
    with pytest.raises(DeviceConflictError):
        service.audio(request)


def test_forged_ranges_stale_signature_and_source_invalidation(actual_audio):
    f, _, _ = actual_audio
    ids = short_rows(f)
    pid, c = pending_sample(f, ids)
    service = DeviceReviewService(f.core)
    item = next(i for i in service.snapshot()['items'] if c['prototype_id'] in i['context'].get('prototype_ids', []))
    request = dict(review_id=item['review_id'], prototype_id=c['prototype_id'])
    for field, value in [('media_id', 'secret'), ('start_ms', 0), ('end_ms', 1), ('window_index', 8)]:
        with pytest.raises(ValueError):
            service.audio({**request, field: value})
    with pytest.raises(DeviceConflictError):
        service.audio({**request, 'audition_key': 'obsolete'})
    classify(f, ids[0], 'non_speech')
    with pytest.raises(DeviceConflictError):
        service.audio(request)
    with pytest.raises((KeyError, ValueError)):
        f.core.people.review_prototype(c['prototype_id'], pid, 'confirmed')


def test_truncated_source_cannot_claim_full_coverage_and_temp_is_cleaned(tmp_path):
    source = tmp_path/'short.wav'
    sf.write(source, np.zeros(1600), 16000)
    with pytest.raises(ValueError, match='complete sample window'):
        _normalized_review_audio(source, 0, 1000, temp_root=tmp_path/'clips')
    assert not list((tmp_path/'clips').iterdir())
