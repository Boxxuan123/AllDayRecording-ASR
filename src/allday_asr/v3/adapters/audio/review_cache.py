"""Private, bounded derived WAV cache. Authorization belongs to the caller.

One atomic file contains both its descriptor and verified bytes. Waiting HTTP
requests share a Future; no cache lock is held while rendering or reading audio.
"""
from concurrent.futures import Future
import hashlib
import json
import logging
import os
from pathlib import Path
import threading
import time
from uuid import uuid4


class ReviewAudioCache:
    MAX_BYTES = 128 * 1024 * 1024
    MAX_ITEMS = 64
    MAX_AUDIO = 16 * 1024 * 1024
    MAX_PENDING = 16

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.condition = threading.Condition()
        self.entries = {}
        self.inflight = {}
        self.queue = []
        self.pins = {}
        self.reservations = {}
        self.garbage = {}
        self.cleanup_lock = threading.Lock()
        self.cleanup_after = 0.0
        self.last_cleanup_log = float("-inf")
        self.live_workers = 2
        self.sequence = 0
        self.closed = False
        self.stats = dict(hits=0, renders=0, joins=0, failures=0)
        self.workers = []
        # Bounded crash recovery; clicks never scan the directory.
        for index, path in enumerate(root.iterdir()):
            if index >= 256:
                break
            if path.suffix == '.part':
                self.garbage[path] = path.stat().st_size
            elif path.suffix == '.entry' and len(path.stem) == 64:
                stat = path.stat()
                self.entries[path.stem] = (stat.st_size, stat.st_mtime)
        self._evict()
        for index in range(2):
            worker = threading.Thread(target=self._worker, name=f'review-audio-{index}', daemon=True)
            self.workers.append(worker)
            worker.start()

    def get(self, key, render, *, prefetch=False):
        if len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
            raise ValueError('invalid audio content identity')
        with self.condition:
            if self.closed:
                raise RuntimeError('audio preparation stopped')
            if self.live_workers == 0:
                raise RuntimeError('audio preparation workers unavailable')
            existing = self.inflight.get(key)
            if existing is not None:
                future, job = existing
                if not prefetch:
                    job[0] = 0  # Promote a queued prefetch instead of rendering twice.
                self.stats['joins'] += 1
            else:
                if len(self.inflight) >= self.MAX_PENDING:
                    raise RuntimeError('audio preparation busy; retry shortly')
                future = Future()
                self.sequence += 1
                job = [1 if prefetch else 0, self.sequence, key, render, future]
                self.inflight[key] = (future, job)
                self.queue.append(job)
                self.condition.notify()
        # A caller stopping its wait must not cancel other waiters' shared work.
        return future.result(timeout=120)

    def _worker(self):
        try:
            self._work_loop()
        finally:
            with self.condition:
                self.live_workers -= 1
                if not self.live_workers:
                    pending, self.queue = self.queue, []
                    for _, _, key, _, future in pending:
                        self.inflight.pop(key, None)
                        future.set_exception(RuntimeError('audio preparation workers unavailable'))
                    self.condition.notify_all()

    def _work_loop(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.queue or self.closed)
                if not self.queue:
                    return
                job = min(self.queue, key=lambda item: (item[0], item[1]))
                self.queue.remove(job)
            _, _, key, render, future = job
            try:
                data = self._read(key)
                hit = data is not None
                if data is None:
                    self._reserve(key)
                    data = render()
                    if not 0 < len(data) <= self.MAX_AUDIO:
                        raise ValueError('review audio exceeds cache item budget')
                    self._publish(key, data)
                with self.condition:
                    self.stats['hits' if hit else 'renders'] += 1
                future.set_result((data, hit))
            except BaseException as error:
                with self.condition:
                    self.stats['failures'] += 1
                future.set_exception(error if isinstance(error, Exception) else RuntimeError('audio worker exited'))
                if not isinstance(error, Exception):
                    raise
            finally:
                with self.condition:
                    self.inflight.pop(key, None)
                    self.reservations.pop(key, None)
                self._evict()

    def _read(self, key):
        path = self.root / f'{key}.entry'
        with self.condition:
            self.pins[key] = self.pins.get(key, 0) + 1
        try:
            with path.open('rb') as stream:
                header = json.loads(stream.readline(2048))
                data = stream.read(self.MAX_AUDIO + 1)
            if (header['key'] != key or header['length'] != len(data)
                    or not 0 < len(data) <= self.MAX_AUDIO or hashlib.sha256(data).hexdigest() != header['sha256']):
                raise ValueError('invalid cached audio')
            with self.condition:
                self.entries[key] = (path.stat().st_size, time.time())
            return data
        except (OSError, ValueError, KeyError, TypeError):
            path.unlink(missing_ok=True)
            with self.condition:
                self.entries.pop(key, None)
            return None
        finally:
            with self.condition:
                self.pins[key] -= 1

    def _publish(self, key, data):
        header = json.dumps(dict(key=key, length=len(data), sha256=hashlib.sha256(data).hexdigest())).encode() + b'\n'
        temp = self.root / f'{uuid4().hex}.part'
        destination = self.root / f'{key}.entry'
        try:
            with temp.open('xb') as stream:
                stream.write(header)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, destination)
            with self.condition:
                self.entries[key] = (len(header) + len(data), time.time())
                self.reservations.pop(key, None)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError as error:
                # Conservatively retain the entire reserved size if stat is denied.
                with self.condition:
                    self.garbage[temp] = len(header) + len(data)
                self._cleanup_error(error)


    def _cleanup_error(self, error):
        with self.condition:
            now = time.monotonic()
            self.cleanup_after = now + 1.0
            if now - self.last_cleanup_log >= 60:
                self.last_cleanup_log = now
                # Never log paths, content or raw exception messages.
                logging.getLogger(__name__).warning(
                    'review audio cleanup deferred: category=%s errno=%s',
                    type(error).__name__, error.errno)

    def _fits(self, size=0, items=0):
        return (sum(v[0] for v in self.entries.values()) + sum(self.garbage.values())
                + sum(self.reservations.values()) + size <= self.MAX_BYTES
                and len(self.entries) + len(self.garbage) + len(self.reservations) + items <= self.MAX_ITEMS)

    def _reserve(self, key):
        # Reserve before rendering/writing: cleanup failure cannot grow disk use.
        size = self.MAX_AUDIO + 2048
        self._evict(size, 1)
        with self.condition:
            if not self._fits(size, 1):
                raise RuntimeError('audio cache capacity unavailable; cleanup pending or entries in use')
            self.reservations[key] = size

    def _evict(self, size=0, items=0):
        # One bounded cleanup pass; do not wait under the admission lock.
        with self.cleanup_lock:
            with self.condition:
                if time.monotonic() < self.cleanup_after:
                    return
                victims = list(self.garbage)
            for path in victims:
                try:
                    path.unlink(missing_ok=True)
                except OSError as error:
                    self._cleanup_error(error)
                else:
                    with self.condition:
                        self.garbage.pop(path, None)
            with self.condition:
                candidates = sorted(self.entries, key=lambda key: self.entries[key][1])
            for key in candidates:
                with self.condition:
                    if self._fits(size, items):
                        break
                    if key not in self.entries or key in self.inflight or self.pins.get(key, 0):
                        continue
                    original = self.root / f'{key}.entry'
                    trash = self.root / f'{uuid4().hex}.part'
                    try:
                        os.replace(original, trash)
                    except FileNotFoundError:
                        self.entries.pop(key)
                        continue
                    except OSError as error:
                        self._cleanup_error(error)
                        break
                    entry_size, _ = self.entries.pop(key)
                    self.garbage[trash] = entry_size
                try:
                    trash.unlink(missing_ok=True)
                except OSError as error:
                    self._cleanup_error(error)
                    break
                else:
                    with self.condition:
                        self.garbage.pop(trash, None)

    def close(self):
        with self.condition:
            self.closed = True
            pending, self.queue = self.queue, []
            for _, _, key, _, future in pending:
                self.inflight.pop(key, None)
                future.set_exception(RuntimeError('audio preparation stopped'))
            self.condition.notify_all()
        for worker in self.workers:
            worker.join(timeout=35)


class ReviewAudioCacheOwner:
    """One lazy worker pool per application core; composition performs no IO."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cache = None
        self.closed = False

    def get(self, root):
        with self.lock:
            if self.closed:
                raise RuntimeError('audio preparation stopped')
            if self.cache is None:
                self.cache = ReviewAudioCache(root)
            return self.cache

    def close(self):
        with self.lock:
            self.closed = True
            if self.cache is not None:
                self.cache.close()
