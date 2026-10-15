"""Regression coverage for Windows sharing locks on native heartbeat IPC."""

import ctypes
import json
import sys
import threading
from types import SimpleNamespace

import pytest

from allday_asr.v3.adapters.models import isolated_native as native


def make_watch(tmp_path, *, progress_seconds=10):
    checkpoints = []
    control = SimpleNamespace(
        heartbeat=lambda checkpoint: checkpoints.append(checkpoint) or False,
        cancellation_requested=lambda: False,
    )
    path = tmp_path / "progress.json"
    path.write_text(json.dumps({"sequence": 1, "checkpoint": {"window": 7}}))
    watch = native.ProgressWatch(control, threading.Event(), path, progress_seconds)
    return watch, checkpoints


def sharing_error(winerror=32):
    error = PermissionError(13, "Permission denied")
    error.winerror = winerror
    return error


@pytest.mark.skipif(sys.platform != "win32", reason="Windows sharing semantics")
def test_actual_windows_exclusive_lock_recovers_without_losing_checkpoint(tmp_path):
    watch, checkpoints = make_watch(tmp_path)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                  ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                  ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    # No sharing: reading the heartbeat produces the real reported EACCES.
    handle = kernel.CreateFileW(str(watch.path), 0x80000000, 0, None, 3, 0, None)
    assert handle not in (None, ctypes.c_void_p(-1).value)
    try:
        with pytest.raises(PermissionError):
            watch.path.read_text()
        assert not watch.is_set()
        assert checkpoints == []
        assert watch.read_blocked_since is not None
    finally:
        assert kernel.CloseHandle(handle)
    assert not watch.is_set()
    assert checkpoints == [{"window": 7}]
    assert watch.read_blocked_since is None
    assert not watch.is_set()
    assert len(checkpoints) == 1


@pytest.mark.parametrize("winerror", [5, 32, None])
def test_persistent_access_conflict_is_bounded(tmp_path, monkeypatch, winerror):
    clock = [0.0]
    monkeypatch.setattr(native.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(native.sys, "platform", "win32")
    watch, _ = make_watch(tmp_path)
    error = sharing_error(winerror)
    def blocked_read(*args, **kwargs):
        raise error
    monkeypatch.setattr(type(watch.path), "read_text", blocked_read)
    assert not watch.is_set()
    clock[0] = 1.9
    assert not watch.is_set()
    clock[0] = 2.0
    with pytest.raises(PermissionError) as caught:
        watch.is_set()
    assert caught.value is error


def test_locked_file_still_obeys_progress_deadline(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(native.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(native.sys, "platform", "win32")
    watch, _ = make_watch(tmp_path, progress_seconds=1)
    def blocked_read(*args, **kwargs):
        raise sharing_error()
    monkeypatch.setattr(type(watch.path), "read_text", blocked_read)
    assert not watch.is_set()
    clock[0] = 1.1
    assert watch.is_set()
    assert watch.timed_out
    assert watch.last_progress == 0


@pytest.mark.parametrize("cancel_source", ["stop", "control"])
def test_cancellation_does_not_wait_for_progress_file(tmp_path, monkeypatch, cancel_source):
    watch, _ = make_watch(tmp_path)
    def unreadable(*args, **kwargs):
        raise AssertionError("Cancellation must be checked before accessing the file")
    monkeypatch.setattr(type(watch.path), "read_text", unreadable)
    if cancel_source == "stop":
        watch.stop.set()
    else:
        watch.control.cancellation_requested = lambda: True
    assert watch.is_set()


def test_missing_heartbeat_does_not_reset_progress_deadline(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(native.time, "monotonic", lambda: clock[0])
    watch, checkpoints = make_watch(tmp_path, progress_seconds=1)
    watch.path.unlink()
    assert not watch.is_set()
    clock[0] = 1.1
    assert watch.is_set()
    assert watch.timed_out
    assert checkpoints == []


def test_unrelated_access_error_is_not_hidden(tmp_path, monkeypatch):
    monkeypatch.setattr(native.sys, "platform", "win32")
    watch, _ = make_watch(tmp_path)
    error = sharing_error(65)
    def denied(*args, **kwargs):
        raise error
    monkeypatch.setattr(type(watch.path), "read_text", denied)
    with pytest.raises(PermissionError) as caught:
        watch.is_set()
    assert caught.value is error
