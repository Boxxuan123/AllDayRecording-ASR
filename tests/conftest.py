"""Keep generated test state ephemeral, even on setup or assertion failure."""
import errno
from pathlib import Path
import sys

import pytest

from tests.temp_workspace import TestDirectory, cleanup_scope


_cleanup_key = pytest.StashKey()


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    # Never hand an existing data/report directory to pytest: it recursively
    # deletes explicit basetemp before use. Give it a fresh owned child instead.
    parent = (
        Path(config.option.basetemp)
        if config.option.basetemp
        else Path(__file__).resolve().parents[1] / "outputs" / ".test-tmp"
    ).resolve()
    directory = TestDirectory(parent, "pytest-run")
    created_parent = not parent.exists()
    parent.mkdir(parents=True, exist_ok=True)
    config.option.basetemp = str(directory.path)

    cleaned = False

    def cleanup():
        nonlocal cleaned
        if cleaned:
            return
        cleaned = True
        directory.cleanup()
        if created_parent:
            try:
                parent.rmdir()  # Only an empty container; never recursive deletion.
            except FileNotFoundError:
                pass
            except OSError as exc:
                # Another concurrent test run may still own a child directory.
                if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                    raise

    # Runs after session fixture teardown and report/JUnit hooks, also on errors.
    config.stash[_cleanup_key] = cleanup
    config.add_cleanup(cleanup)
    sys.dont_write_bytecode = True


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionfinish(session):
    try:
        yield
    finally:
        cleanup = session.config.stash.get(_cleanup_key, None)
        if cleanup is not None:
            try:
                cleanup()
            except Exception as exc:
                # Distinguish cleanup failure from expected assertion failures
                # in baseline reproduction tools; leave JUnit/report files intact.
                pytest.exit(f"test workspace cleanup failed: {exc}", returncode=3)


@pytest.fixture(autouse=True)
def _test_resource_cleanup(request):
    with cleanup_scope(request.addfinalizer):
        yield
