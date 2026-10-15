"""Cross-process finite file lock for the daily worker and backfill marker."""

import os
import time
from contextlib import contextmanager
from pathlib import Path


def replace_atomic(source, destination, *, timeout_seconds=2):
    """Bound transient Windows sharing/scan locks without hiding durable errors."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            os.replace(source, destination)
            return
        except PermissionError as exc:
            if os.name != 'nt' or exc.winerror not in {5, 32} or time.monotonic() >= deadline:
                raise
            time.sleep(0.02)

@contextmanager
def day_worker_lock(database_path):
    path = Path(database_path).with_suffix(".daily-generation.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    acquired = False
    try:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except (OSError, BlockingIOError):
            pass
        yield acquired
    finally:
        if acquired:
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
