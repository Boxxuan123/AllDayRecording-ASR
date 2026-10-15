"""Regression coverage for cleanup failures and preservation of external evidence."""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path

import pytest

from tests import temp_workspace
from tests.temp_workspace import TestDirectory, cleanup_test_case, test_directory


@pytest.mark.parametrize("failure", ["none", "setup", "assertion"])
def test_unittest_closes_database_before_removing_directory(tmp_path, failure):
    allocated = []
    closed = []

    class Case(unittest.TestCase):
        def setUp(self):
            self.root = test_directory(self, tmp_path, "unittest")
            allocated.append(self.root)
            self.root.mkdir()
            connection = sqlite3.connect(self.root / "core.sqlite3")
            connection.execute("CREATE TABLE fixture(value)")

            def close():
                connection.close()
                closed.append(True)

            self.addCleanup(close)
            if failure == "setup":
                raise RuntimeError("synthetic setup failure")

        def tearDown(self):
            cleanup_test_case(self)

        def runTest(self):
            if failure == "assertion":
                self.fail("synthetic assertion failure")

    result = unittest.TestResult()
    Case().run(result)
    assert len(result.errors) == int(failure == "setup")
    assert len(result.failures) == int(failure == "assertion")
    assert closed == [True]
    assert not allocated[0].exists()


def test_transient_windows_lock_is_retried_without_touching_neighbors(tmp_path, monkeypatch):
    report = tmp_path / "report.json"
    report.write_bytes(b"keep report")
    directory = TestDirectory(tmp_path, "retry")
    directory.path.mkdir()
    (directory.path / "fixture").write_bytes(b"temporary")
    remove = shutil.rmtree
    attempts = []

    def locked_once(path):
        attempts.append(path)
        if len(attempts) == 1:
            exc = PermissionError("synthetic sharing violation")
            exc.winerror = 32
            raise exc
        remove(path)

    monkeypatch.setattr(temp_workspace.shutil, "rmtree", locked_once)
    monkeypatch.setattr(temp_workspace.time, "sleep", lambda _: None)
    directory.cleanup()
    directory.cleanup()  # Repeated unittest/pytest finalization is harmless.
    assert len(attempts) == 3
    assert report.read_bytes() == b"keep report"
    assert not directory.path.exists()


def test_persistent_lock_is_visible_and_leaves_the_owned_path(tmp_path, monkeypatch):
    directory = TestDirectory(tmp_path, "locked")
    directory.path.mkdir()
    with monkeypatch.context() as patch:
        def locked(_):
            raise PermissionError(13, "still in use")

        patch.setattr(temp_workspace.shutil, "rmtree", locked)
        patch.setattr(temp_workspace.time, "sleep", lambda _: None)
        with pytest.raises(RuntimeError, match="cleanup failed after retries"):
            directory.cleanup()
    assert directory.path.exists()
    directory.cleanup()


@pytest.mark.parametrize("prefix", ["../state", "", "data/backup", ".."])
def test_cannot_allocate_a_parent_or_existing_data_directory(tmp_path, prefix):
    with pytest.raises(ValueError):
        TestDirectory(tmp_path, prefix)


def test_redirected_root_is_rejected_and_external_data_survives(tmp_path, monkeypatch):
    directory = TestDirectory(tmp_path, "redirected")
    directory.path.mkdir()
    external = tmp_path / "production"
    external.mkdir()
    (external / "core.sqlite3").write_bytes(b"real data")
    resolve = Path.resolve

    def redirected(path, *args, **kwargs):
        return external if path == directory.path else resolve(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", redirected)
        with pytest.raises(RuntimeError, match="refusing redirected"):
            directory.cleanup()
    assert (external / "core.sqlite3").read_bytes() == b"real data"
    directory.cleanup()


CASES = {
    "pass": "def test_case(tmp_path): (tmp_path / 'payload').write_bytes(b'test')",
    "fail": "def test_case(tmp_path):\n    (tmp_path / 'payload').write_bytes(b'test')\n    assert False",
    "setup-fail": """@pytest.fixture
def broken(tmp_path):
    (tmp_path / 'payload').write_bytes(b'test')
    raise RuntimeError('synthetic setup error')
def test_case(broken): pass""",
    "manual-setup-fail": """class Manual(unittest.TestCase):
    def setUp(self):
        self.root = test_directory(self, Path(os.environ['OWNED_PARENT']), 'manual')
        self.root.mkdir()
        connection = sqlite3.connect(self.root / 'core.sqlite3')
        self.addCleanup(connection.close)
        raise RuntimeError('synthetic manual setup error')
    def runTest(self): pass
@pytest.fixture
def broken():
    case = Manual()
    case.setUp()
    yield case
def test_case(broken): pass""",
    "collection-fail": "raise RuntimeError('synthetic collection error')",
    "interrupt": "def test_case(tmp_path):\n    (tmp_path / 'payload').write_bytes(b'test')\n    raise KeyboardInterrupt()",
}


@pytest.mark.parametrize("scenario", list(CASES))
@pytest.mark.parametrize("explicit_basetemp", [True, False])
def test_pytest_cleanup_preserves_requested_basetemp_reports_and_backups(tmp_path, scenario, explicit_basetemp):
    suite = tmp_path / "suite"
    suite.mkdir()
    requested = tmp_path / "evidence" / "pytest-temp"
    requested.mkdir(parents=True)
    retained = {
        requested / "real.sqlite3": b"production data",
        requested / "backup.zip": b"retained backup",
        requested / "report.json": b"retained report",
        requested.parent / "acceptance.json": b"acceptance evidence",
    }
    for path, content in retained.items():
        path.write_bytes(content)
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in retained}
    root = Path(__file__).resolve().parents[1]
    shutil.copyfile(root / "tests/conftest.py", suite / "conftest.py")
    header = """import os, sqlite3, unittest
from pathlib import Path
import pytest
from tests.temp_workspace import test_directory
"""
    (suite / "test_probe.py").write_text(header + CASES[scenario], encoding="utf-8")
    junit = requested.parent / "pytest.xml"
    command = [sys.executable, "-m", "pytest", "-q", str(suite), "-c", str(root / "pyproject.toml"),
               "--junitxml", str(junit)]
    if explicit_basetemp:
        command.extend(["--basetemp", str(requested)])
    result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=45,
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                                 "OWNED_PARENT": str(requested)}, check=False)
    assert result.returncode == (0 if scenario == "pass" else 2 if scenario in ("collection-fail", "interrupt") else 1), result.stdout + result.stderr
    assert junit.is_file()
    assert not (tmp_path / "outputs" / ".test-tmp").exists()
    assert {p.name for p in requested.iterdir()} == {"real.sqlite3", "backup.zip", "report.json"}
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in retained} == before


def test_failed_session_cleanup_does_not_return_success(tmp_path):
    root = Path(__file__).resolve().parents[1]
    suite = tmp_path / "suite"
    suite.mkdir()
    shutil.copyfile(root / "tests/conftest.py", suite / "conftest.py")
    requested = tmp_path / "evidence"
    requested.mkdir()
    report = requested / "report.json"
    report.write_bytes(b"keep report")
    (suite / "test_probe.py").write_text("""from tests import temp_workspace

def test_case(tmp_path):
    (tmp_path / 'fixture').write_bytes(b'test')
    remove = temp_workspace.shutil.rmtree
    def locked(path):
        if path.name.startswith('pytest-run-'):
            raise PermissionError(13, 'synthetic persistent Windows lock')
        remove(path)
    temp_workspace.shutil.rmtree = locked
    temp_workspace.time.sleep = lambda _: None
""", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(suite), "-c", str(root / "pyproject.toml"),
         "--basetemp", str(requested), "--junitxml", str(requested / "pytest.xml")],
        cwd=root, capture_output=True, text=True, timeout=45,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, check=False,
    )
    assert result.returncode == 3
    assert "cleanup failed after retries" in result.stdout + result.stderr
    assert report.read_bytes() == b"keep report"
    assert (requested / "pytest.xml").is_file()
    assert len(list(requested.glob("pytest-run-*"))) == 1


def test_manual_fixture_cleanup_retains_errors_and_attempts_every_callback():
    case = unittest.TestCase()
    attempted = []
    case.addCleanup(lambda: attempted.append("directory cleanup"))

    def failed_close():
        attempted.append("close")
        raise RuntimeError("synthetic open handle")

    case.addCleanup(failed_close)
    with pytest.raises(ExceptionGroup) as error:
        cleanup_test_case(case)
    assert attempted == ["close", "directory cleanup"]
    assert str(error.value.exceptions[0]) == "synthetic open handle"
    cleanup_test_case(case)  # No stale callbacks when pytest retries finalization.
