"""Private, bounded derived WAV cache. Authorization belongs to the caller.

One atomic file contains both its descriptor and verified bytes. Waiting HTTP
requests share a Future; no cache lock is held while rendering or reading audio.
"""
from concurrent.futures import Future
import hashlib
import json
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
        self.sequence = 0
        self.closed = False
        self.stats = dict(hits=0, renders=0, joins=0, failures=0)
        self.workers = []
        # Bounded crash recovery; clicks never scan the directory.
        for index, path in enumerate(root.iterdir()):
            if index >= 256:
                break
            if path.suffix == '.part':
                path.unlink(missing_ok=True)
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
                    data = render()
                    if not 0 < len(data) <= self.MAX_AUDIO:
                        raise ValueError('review audio exceeds cache item budget')
                    self._publish(key, data)
                with self.condition:
                    self.stats['hits' if hit else 'renders'] += 1
                future.set_result((data, hit))
            except Exception as error:
                with self.condition:
                    self.stats['failures'] += 1
                future.set_exception(error)
            finally:
                with self.condition:
                    self.inflight.pop(key, None)
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
        finally:
            temp.unlink(missing_ok=True)

    def _evict(self):
        victims = []
        with self.condition:
            for key, (_size, _) in sorted(self.entries.items(), key=lambda item: item[1][1]):
                if len(self.entries) <= self.MAX_ITEMS and sum(v[0] for v in self.entries.values()) <= self.MAX_BYTES:
                    break
                if key in self.inflight or self.pins.get(key, 0):
                    continue
                # Rename while admission is locked: a new caller cannot mistake
                # an evicted entry for a newly published entry with the same key.
                original = self.root / f'{key}.entry'
                trash = self.root / f'{uuid4().hex}.part'
                try:
                    os.replace(original, trash)
                except FileNotFoundError:
                    pass
                self.entries.pop(key)
                victims.append(trash)
        for path in victims:
            path.unlink(missing_ok=True)

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
