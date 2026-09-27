import base64
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
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


def test_coalescing_priority_and_failure_retry(tmp_path, monkeypatch):
    from allday_asr.v3.adapters.audio import review_cache
    selected = []
    def observe_selection(items, **kwargs):
        chosen = min(items, **kwargs)
        selected.append(chosen[2])
        return chosen
    monkeypatch.setattr(review_cache, 'min', observe_selection, raising=False)
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
        assert selected[-2:] == [key('high'), key('low')]
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


def test_cleanup_failure_preserves_workers_budget_and_recovers(tmp_path, monkeypatch, caplog):
    from allday_asr.v3.adapters.audio import review_cache
    cache = ReviewAudioCache(tmp_path)
    cache.MAX_ITEMS = 2
    clock = [time.monotonic()]
    monkeypatch.setattr(review_cache.time, 'monotonic', lambda: clock[0])
    original = review_cache.os.replace
    def denied(source, target):
        if str(source).endswith('.entry'):
            raise PermissionError(13, 'synthetic')
        return original(source, target)
    try:
        cache.get(key('a'), lambda: b'a')
        cache.get(key('b'), lambda: b'b')
        monkeypatch.setattr(review_cache.os, 'replace', denied)
        for number in range(12):
            clock[0] += 2
            with pytest.raises(RuntimeError, match='capacity'):
                cache.get(key(str(number)), lambda: pytest.fail('must reject before render'))
            assert all(w.is_alive() for w in cache.workers)
            assert len(list(tmp_path.glob('*.entry'))) == 2
            assert len(cache.entries) == 2
            assert len(cache.inflight) <= 1
            assert not cache.reservations
            assert not cache.pins
        assert cache.get(key('a'), lambda: pytest.fail('unrelated cleanup invalidated hit')) == (b'a', True)
        assert len([r for r in caplog.records if 'cleanup deferred' in r.message]) == 1
        monkeypatch.setattr(review_cache.os, 'replace', original)
        clock[0] += 2
        with ThreadPoolExecutor(1) as pool:
            assert pool.submit(cache.get, key('recovered'), lambda: b'ok').result(3) == (b'ok', False)
    finally:
        cache.close()
    assert not cache.inflight and not cache.queue and not cache.reservations


def test_failed_trash_unlink_still_counts_disk_and_close(tmp_path, monkeypatch):
    cache = ReviewAudioCache(tmp_path)
    cache.MAX_ITEMS = 1
    cache.get(key('old'), lambda: b'old')
    original = Path.unlink
    def denied(path, *args, **kwargs):
        if path.suffix == '.part' and path.exists():
            raise PermissionError(13, 'synthetic')
        return original(path, *args, **kwargs)
    try:
        monkeypatch.setattr(Path, 'unlink', denied)
        with pytest.raises(RuntimeError, match='capacity'):
            cache.get(key('new'), lambda: pytest.fail('unreclaimed bytes'))
        assert len(cache.garbage) == 1
        assert sum(cache.garbage.values()) == sum(p.stat().st_size for p in tmp_path.iterdir())
        assert all(w.is_alive() for w in cache.workers)
        monkeypatch.setattr(Path, 'unlink', original)
        cache.cleanup_after = 0
        assert cache.get(key('new'), lambda: b'new') == (b'new', False)
    finally:
        cache.close()
    with pytest.raises(RuntimeError, match='stopped'):
        cache.get(key('closed'), lambda: b'no')


def test_unavailable_pool_rejects_without_queued_future(tmp_path):
    cache = ReviewAudioCache(tmp_path)
    cache.close()
    cache.closed = False  # Model an unexpectedly exhausted pool, without 120s waiting.
    assert cache.live_workers == 0
    with pytest.raises(RuntimeError, match='workers unavailable'):
        cache.get(key('unavailable'), lambda: pytest.fail('no worker'))
    assert not cache.queue and not cache.inflight


def test_publish_failure_releases_reservation_and_retry(tmp_path, monkeypatch):
    from allday_asr.v3.adapters.audio import review_cache
    cache = ReviewAudioCache(tmp_path)
    original = review_cache.os.replace
    def denied(source, target):
        if str(target).endswith('.entry'):
            raise PermissionError(13, 'synthetic publish failure')
        return original(source, target)
    try:
        monkeypatch.setattr(review_cache.os, 'replace', denied)
        with pytest.raises(PermissionError):
            cache.get(key('publish'), lambda: b'bytes')
        monkeypatch.setattr(review_cache.os, 'replace', original)
        assert cache.get(key('publish'), lambda: b'bytes') == (b'bytes', False)
        assert all(w.is_alive() for w in cache.workers)
    finally:
        cache.close()
    assert not cache.reservations and not cache.inflight
    assert not list(tmp_path.glob('*.part'))


def test_close_fails_queued_waiter_and_drains_running_jobs(tmp_path):
    cache = ReviewAudioCache(tmp_path)
    gate = threading.Event()
    started = threading.Barrier(3)
    def render():
        started.wait(3)
        assert gate.wait(3)
        return b'running'
    with ThreadPoolExecutor(4) as pool:
        running = [pool.submit(cache.get, key('active' + str(n)), render) for n in range(2)]
        started.wait(3)
        queued = pool.submit(cache.get, key('queued'), lambda: pytest.fail('closed queue rendered'))
        deadline = time.monotonic() + 3
        while not cache.queue:
            assert time.monotonic() < deadline
            time.sleep(.001)
        closing = pool.submit(cache.close)
        try:
            with pytest.raises(RuntimeError, match='stopped'):
                queued.result(3)
        finally:
            gate.set()
        for future in running:
            assert future.result(3) == (b'running', False)
        closing.result(3)
    assert not cache.inflight and not cache.queue and not cache.reservations
    assert cache.live_workers == 0
