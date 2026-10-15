"""Task-owned native stage process; results publish only through the parent UOW.

Pickle files are private IPC created by this parent, never imported user data.
Windows Job kill-on-close also fences abrupt receiver process death.
"""
import errno
import importlib
import json
import os
import pickle
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from allday_asr.v3.ports.processing import StageCancellationRequested
from allday_asr.v3.application.file_lock import replace_atomic


class ProgressWatch:
    def __init__(self, control, stop, path, progress_seconds):
        self.control, self.stop, self.path = control, stop, path
        self.progress_seconds = progress_seconds
        self.last_progress = time.monotonic()
        self.sequence = None
        self.timed_out = False
        self.read_blocked_since = None

    def is_set(self):
        # The child atomically replaces this file. Windows can briefly deny
        # opening it during replacement or scanning; do not fail a whole ASR
        # stage for a transient sharing lock. Keep polling cancellation and
        # deadlines while allowing at most two seconds for the lock to clear.
        if self.stop.is_set() or self.control.cancellation_requested():
            return True
        try:
            value = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            value = None  # No first heartbeat yet, or replacement in progress.
        except PermissionError as exc:
            winerror = getattr(exc, 'winerror', None)
            # Python's buffered file open can expose only errno=EACCES, even
            # for a real Windows sharing violation. Durable denial still fails
            # once the bounded retry interval expires.
            transient = winerror in {5, 32} or (winerror is None and exc.errno == errno.EACCES)
            if sys.platform != 'win32' or not transient:
                raise
            now = time.monotonic()
            if self.read_blocked_since is None:
                self.read_blocked_since = now
            if now - self.read_blocked_since >= 2:
                raise
            value = None
        else:
            self.read_blocked_since = None
        if value is not None:
            if value['sequence'] != self.sequence:
                self.sequence = value['sequence']
                self.last_progress = time.monotonic()
                if self.control.heartbeat(value['checkpoint']):
                    return True
        self.timed_out = time.monotonic() - self.last_progress > self.progress_seconds
        return self.timed_out or self.stop.is_set() or self.control.cancellation_requested()


def run_owned_stage(entrypoint, payload, control, stop, root, *, stage_seconds, progress_seconds):
    if sys.platform != 'win32':
        raise RuntimeError('SAFE_TIMEOUT_CONTROL_UNAVAILABLE')
    from allday_asr.v3.adapters.codex.owned_attempt import OwnedAttempt
    root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='native-stage-', dir=root) as directory:
        folder = Path(directory)
        request, output, progress = (folder / n for n in ('request.pickle', 'result.pickle', 'progress.json'))
        request.write_bytes(pickle.dumps((entrypoint, payload)))
        watch = ProgressWatch(control, stop, progress, progress_seconds)
        attempt = OwnedAttempt([sys.executable, '-X', 'utf8', '-m', __name__, str(request), str(output), str(progress)],
                               folder / 'process.log', Path.cwd())
        code = attempt.wait(stage_seconds, watch)
        if watch.timed_out or code == 124:
            raise TimeoutError('native stage/progress deadline exceeded; owned resources terminated')
        if code == 125:
            raise StageCancellationRequested('native stage stopped for cancellation or shutdown')
        if code != 0 or not output.exists():
            raise RuntimeError('owned native worker failed')
        result = pickle.loads(output.read_bytes())
        if result['status'] != 'succeeded':
            kind = result['error_type']
            exception = {'ValueError': ValueError, 'TypeError': TypeError}.get(kind, RuntimeError)
            raise exception('native worker failed: ' + kind)
        return result['result']


class IsolatedNativeAdapter:
    def __init__(self, database, audio_store, config, requested_profile, model_path, artifact_store_path=None):
        self.payload = (database.path, audio_store.root, config, requested_profile, model_path,
                        artifact_store_path or database.path.parent / 'artifacts')
        self.root = database.path.parent / 'native-execution'
        self.config = config
        self.stop = threading.Event()

    def execute(self, context, control):
        if self.stop.is_set():
            raise StageCancellationRequested('native adapter is closed')
        run = context.claim.run
        duration = max(1, (run.input_revision or 1))  # A floor before source metadata is loaded.
        from allday_asr.v3.adapters.sqlite import V3Database
        with V3Database(self.payload[0]).read() as connection:
            row = connection.execute('SELECT MAX(session_end_ms) FROM capture_segments WHERE session_id=?', (run.session_id,)).fetchone()
            duration = max(duration, (row[0] or 0) / 1000)
        # Cold model startup + at most 12x audio duration for dual ASR on slower devices.
        stage = min(86400, 900 + 12 * duration)
        progress = (900 + 12 * (self.config.asr.window_seconds + 2 * self.config.asr.context_seconds)
                    if context.claim.stage.stage == 'asr_and_alignment' else 900 + 6 * duration)
        return run_owned_stage(__name__ + ':execute_native', (self.payload, context), control, self.stop,
                               self.root, stage_seconds=stage, progress_seconds=min(stage, progress))

    def close(self):
        self.stop.set()


def execute_native(payload, control):
    from allday_asr.v3.adapters.files import ContentAddressedStore
    from allday_asr.v3.adapters.sqlite import V3Database
    from .native_factory import build_native_model_pipeline
    (database, audio, config, profile, model_path, artifacts), context = payload
    adapter = build_native_model_pipeline(V3Database(database), ContentAddressedStore(audio), config,
        requested_profile=profile, diarization_model_path=model_path, _in_child=True)
    control.heartbeat({'phase': 'stage_started'})
    result = adapter.execute(context, control)
    control.heartbeat({'phase': 'artifact_write'})
    from dataclasses import replace
    store = ContentAddressedStore(artifacts)
    staged = tuple(store.put_bytes(output.payload) for output in result.artifacts)
    return replace(result, stored_artifacts=staged)


class ChildControl:
    def __init__(self, path):
        self.path, self.sequence = path, 0

    def heartbeat(self, checkpoint=None):
        self.sequence += 1
        part = self.path.with_suffix('.part')
        part.write_text(json.dumps({'sequence': self.sequence, 'checkpoint': checkpoint}), encoding='utf-8')
        replace_atomic(part, self.path)
        return False

    def cancellation_requested(self):
        return False


def worker(request, output, progress):
    module, payload = pickle.loads(request.read_bytes())
    name, function = module.split(':')
    try:
        callback = getattr(importlib.import_module(name), function)
        # Native libraries may print transcript/prompt/path; private IPC carries only typed results.
        with open(os.devnull, 'w') as sink:
            from contextlib import redirect_stdout, redirect_stderr
            with redirect_stdout(sink), redirect_stderr(sink):
                result = callback(payload, ChildControl(progress))
        value = {'status': 'succeeded', 'result': result}
    except BaseException as exc:
        value = {'status': 'failed', 'error_type': type(exc).__name__}
    part = output.with_suffix('.part')
    part.write_bytes(pickle.dumps(value))
    replace_atomic(part, output)


if __name__ == '__main__':
    worker(*(Path(p) for p in sys.argv[1:]))
