import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import threading
import time

import pytest

from allday_asr.v3.adapters.audio.review_cache import ReviewAudioCache
from allday_asr.v3.interfaces.device_reviews import DeviceReviewService
from allday_asr.v3.interfaces.transfer.devices import DeviceConflictError
from tests.test_review_audio_boundaries import actual_audio as actual_audio, pending_sample
from tests.test_phase1_human_facts import people as people
from tests.test_phase2_samples import short_rows


def key(value):
    return hashlib.sha256(value.encode()).hexdigest()


def test_restart_corruption_missing_partial_and_bounds(tmp_path):
    cache = ReviewAudioCache(tmp_path)
    assert cache.get(key('one'), lambda: b'synthetic') == (b'synthetic', False)
    cache.close()
    (tmp_path / 'interrupted.part').write_bytes(b'incomplete')
    cache = ReviewAudioCache(tmp_path)
    assert not (tmp_path / 'interrupted.part').exists()
    assert cache.get(key('one'), lambda: pytest.fail('must persist')) == (b'synthetic', True)
    (tmp_path / f'{key("one")}.entry').write_bytes(b'broken')
    assert cache.get(key('one'), lambda: b'repaired') == (b'repaired', False)
    (tmp_path / f'{key("one")}.entry').unlink()
    assert cache.get(key('one'), lambda: b'restored')[0] == b'restored'
    cache.MAX_ITEMS = 2
    for index in range(6):
        cache.get(key(str(index)), lambda: b'x')
    cache.close()
    assert len(list(tmp_path.glob('*.entry'))) <= 2
    assert not list(tmp_path.glob('*.part'))


def test_coalescing_priority_and_failure_retry(tmp_path):
    cache = ReviewAudioCache(tmp_path)
    gate = threading.Event()
    started = threading.Barrier(3)
    order = []
    def slow():
        started.wait(timeout=5)
        assert gate.wait(5)
        return b'blocking'
    with ThreadPoolExecutor(8) as pool:
        blocking = [pool.submit(cache.get, key(f'block{i}'), slow) for i in range(2)]
        started.wait(timeout=5)
        low = pool.submit(cache.get, key('low'), lambda: order.append('low') or b'low', prefetch=True)
        high = pool.submit(cache.get, key('high'), lambda: order.append('high') or b'high', prefetch=True)
        deadline = time.monotonic() + 5
        while len(cache.queue) < 2:
            assert time.monotonic() < deadline
            time.sleep(.001)
        joined = pool.submit(cache.get, key('high'), lambda: pytest.fail('duplicate render'))
        while not cache.stats['joins']:
            assert time.monotonic() < deadline
            time.sleep(.001)
        gate.set()
        for future in blocking + [low, high, joined]:
            future.result(timeout=5)
        assert high.result() == joined.result()
        assert cache.stats['renders'] == 4
    with pytest.raises(ValueError, match='synthetic'):
        cache.get(key('failed'), lambda: (_ for _ in ()).throw(ValueError('synthetic')))
    assert cache.get(key('failed'), lambda: b'retry') == (b'retry', False)
    cache.close()


def test_real_render_descriptor_hit_and_authority(actual_audio, monkeypatch):
    fixture, _, _ = actual_audio
    person, candidate = pending_sample(fixture, short_rows(fixture))
    service = DeviceReviewService(fixture.core)
    item = next(i for i in service.snapshot()['items'] if candidate['prototype_id'] in i['context'].get('prototype_ids', []))
    public = item['context']['voice_candidates'][0]
    request = dict(review_id=item['review_id'], prototype_id=candidate['prototype_id'],
                   audio_content_key=public['audio_content_key'], audition_key=public['audition_key'])
    first = service.audio(request)
    raw = base64.urlsafe_b64decode(first['data_base64url'] + '==')
    assert first['sha256'] == hashlib.sha256(raw).hexdigest()
    assert first['byte_length'] == len(raw)
    assert first['cache_hit'] is False
    monkeypatch.setattr(service, '_render_bytes', lambda _: pytest.fail('cache hit rerendered'))
    assert service.audio(request)['cache_hit'] is True
    with pytest.raises(DeviceConflictError):
        service.audio({**request, 'audio_content_key': key('stale')})
    fixture.core.people.review_prototype(candidate['prototype_id'], person, 'confirmed')
    with pytest.raises(DeviceConflictError):
        service.audio(request)
    # Same audio under a different grant/review reuses bytes, never the old grant.
    request['review_id'] = f"voice-grant:{candidate['prototype_id']}:{person}"
    assert service.audio(request)['cache_hit'] is True
    source = fixture.core.audio_store.path_for(fixture.core.desktop.media('media-1')['storage_key'])
    source.unlink()
    with pytest.raises((FileNotFoundError, DeviceConflictError)):
        service.audio(request)
