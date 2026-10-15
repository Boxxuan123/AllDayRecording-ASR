"""Own only newly allocated test directories; never sweep data or report roots."""
from __future__ import annotations

import errno
import re
import shutil
import time
import unittest
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from uuid import uuid4


TEST_WORKSPACE_ROOT = Path(__file__).resolve().parents[1] / "outputs" / ".test-workspaces"


_finalizer: ContextVar[Callable[[Callable[[], None]], None] | None] = ContextVar(
    "test_workspace_finalizer", default=None
)


class TestDirectory:
    """Reserve a fresh path without creating it (some tests assert no I/O)."""

    __test__ = False

    def __init__(self, parent: Path, prefix: str) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", prefix):
            raise ValueError("test directory prefix must be a simple name")
        self.path = parent.resolve() / f"{prefix}-{uuid4().hex}"
        if self.path.exists() or self.path.is_symlink():
            raise FileExistsError(self.path)

    def cleanup(self) -> None:
        # Reject a replaced root/ancestor, including a Windows junction. rmtree
        # itself does not follow symlinks or junctions inside the owned root.
        if self.path.resolve() != self.path or self.path.is_symlink():
            raise RuntimeError(f"refusing redirected test directory: {self.path}")
        for attempt in range(6):
            try:
                shutil.rmtree(self.path)
                return
            except FileNotFoundError:
                if not self.path.exists():
                    return
                raise
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EBUSY, errno.EPERM) and getattr(
                    exc, "winerror", None
                ) not in (5, 32, 33):
                    raise
                if attempt == 5:
                    raise RuntimeError(
                        f"test directory cleanup failed after retries: {self.path}"
                    ) from exc
                time.sleep(0.05 * 2**attempt)


def cleanup_test_case(case: unittest.TestCase) -> None:
    # Also supports fixtures that call setUp/tearDown directly instead of run().
    if case._outcome is not None:
        case.doCleanups()  # unittest records each failure in the active result.
        return
    # Manual fixture calls have no unittest result to record errors in. Run the
    # same LIFO stack, attempt every close/remove, and retain the actual errors.
    errors = []
    while case._cleanups:
        callback, args, kwargs = case._cleanups.pop()
        try:
            callback(*args, **kwargs)
        except BaseException as exc:
            errors.append(exc)
    if errors:
        raise BaseExceptionGroup("test resource cleanup failed", errors)


def test_directory(case: unittest.TestCase, parent: Path, prefix: str) -> Path:
    directory = TestDirectory(parent, prefix)
    case.addCleanup(directory.cleanup)
    register = _finalizer.get()
    if register is not None:
        # A manually reused unittest fixture can fail before its own try/finally.
        # Pytest still runs this finalizer, including all resource close callbacks.
        register(lambda: cleanup_test_case(case))
    directory.path.parent.mkdir(parents=True, exist_ok=True)
    return directory.path


test_directory.__test__ = False


@contextmanager
def workspace_directory(parent: Path, prefix: str) -> Iterator[Path]:
    directory = TestDirectory(parent, prefix)
    try:
        directory.path.mkdir(parents=True)
        yield directory.path
    finally:
        directory.cleanup()


@contextmanager
def cleanup_scope(register: Callable[[Callable[[], None]], None]) -> Iterator[None]:
    token = _finalizer.set(register)
    try:
        yield
    finally:
        _finalizer.reset(token)
