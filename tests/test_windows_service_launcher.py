from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "service_launcher", ROOT / "tools/allday_service_runtime.py"
)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def source(root: Path, version: int) -> Path:
    base = root / "src/allday_asr"
    for relative in ["", "v3", "v3/adapters", "v3/adapters/sqlite"]:
        folder = base / relative
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "__init__.py").write_text("", encoding="utf-8")
    (base / "v3/__init__.py").write_text(
        "CONTRACT_VERSION='fixture'\nPROJECTION_VERSION=7\n", encoding="utf-8"
    )
    folder = base / "v3/adapters/sqlite/migrations"
    folder.mkdir()
    (folder / "__init__.py").write_text(
        "from types import SimpleNamespace\n"
        f"MIGRATIONS=tuple(SimpleNamespace(version=i,name=f'm{{i}}',"
        f"sha256=f'h{{i}}') for i in range(1,{version + 1}))\n",
        encoding="utf-8",
    )
    return root


def database(data: Path, version: int) -> Path:
    path = data / "state/v3/core.sqlite3"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as con:
        con.execute(
            "CREATE TABLE schema_migrations(version INTEGER,name TEXT,sha256 TEXT)"
        )
        con.executemany(
            "INSERT INTO schema_migrations VALUES(?,?,?)",
            [(i, f"m{i}", f"h{i}") for i in range(1, version + 1)],
        )
    return path


def test_selects_compatible_checkout_without_modifying_database(tmp_path):
    data = source(tmp_path / "original data", 33)
    updated = source(data / ".task-worktrees/daily-ui-round1", 35)
    db = database(data, 35)
    before = db.read_bytes()
    result = launcher.preflight(data, None)
    assert Path(result["source_root"]) == updated
    assert Path(result["package"]).is_relative_to(updated)
    assert result["database_schema"] == result["supported_schema"] == 35
    assert result["data_root"] == str(data)
    assert db.read_bytes() == before


def test_explicit_old_source_fails_instead_of_silent_fallback(tmp_path):
    data = source(tmp_path / "data", 33)
    source(data / ".task-worktrees/daily-ui-round1", 35)
    db = database(data, 35)
    before = db.read_bytes()
    with pytest.raises(RuntimeError, match="newer than code supports"):
        launcher.preflight(data, data)
    assert db.read_bytes() == before


def test_future_schema_is_never_downgraded(tmp_path):
    data = source(tmp_path / "data", 35)
    db = database(data, 36)
    before = db.read_bytes()
    with pytest.raises(RuntimeError, match="newer than code supports"):
        launcher.preflight(data, None)
    assert db.read_bytes() == before


def test_checksum_mismatch_is_not_bypassed(tmp_path):
    data = source(tmp_path / "data", 35)
    db = database(data, 35)
    with sqlite3.connect(db) as con:
        con.execute("UPDATE schema_migrations SET sha256='wrong' WHERE version=12")
    with pytest.raises(RuntimeError, match="checksum/name mismatch"):
        launcher.preflight(data, data)


def test_fresh_data_preflight_does_not_create_database(tmp_path):
    data = source(tmp_path / "data", 35)
    assert launcher.preflight(data, data)["database_schema"] == 0
    assert not (data / "state").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process startup")
def test_dead_service_is_reported_before_timeout(tmp_path):
    script = ROOT / "tools/restart-allday-services.ps1"
    command = """
$ErrorActionPreference='Stop'
$tree=[Management.Automation.Language.Parser]::ParseFile($env:LAUNCHER_SCRIPT,[ref]$null,[ref]$null)
$names=@('Test-TcpPort','Wait-TcpPort')
foreach($name in $names){
 $fn=$tree.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true)
 . ([scriptblock]::Create($fn.Extent.Text))
}
$LogRoot='isolated-test-logs'
$process=Start-Process -FilePath $env:LAUNCHER_PYTHON -ArgumentList '-c "raise SystemExit(9)"' -WindowStyle Hidden -PassThru
$watch=[Diagnostics.Stopwatch]::StartNew()
try { Wait-TcpPort -Port 1 -Process $process -ServiceName Fixture; throw 'expected service exit' }
catch {
 if($_.Exception.Message -notmatch 'Fixture exited with code 9'){throw}
 if($watch.Elapsed.TotalSeconds -gt 10){throw 'did not fail fast'}
 Write-Output 'PASS: service exit detected before timeout'
}
"""
    import os

    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=15,
        env=dict(
            os.environ, LAUNCHER_SCRIPT=str(script), LAUNCHER_PYTHON=sys.executable
        ),
    )
    assert result.returncode == 0, result.stderr
    assert "PASS: service exit" in result.stdout


def test_preflight_cli_reports_import_location(tmp_path):
    data = source(tmp_path / "data", 35)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/allday_service_runtime.py"),
            "preflight",
            "--data-root",
            str(data),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert Path(json.loads(result.stdout)["package"]).is_relative_to(data)


@pytest.mark.parametrize("role", ["receiver", "web"])
@pytest.mark.parametrize("cached_only", [False, True])
def test_services_keep_original_data_and_model_options(
    tmp_path, monkeypatch, role, cached_only
):
    import types

    data = tmp_path / "original data"
    data.mkdir()
    selected = tmp_path / "updated code"
    calls = []
    guards = []
    paths = types.ModuleType("allday_asr.v3.paths")
    package = types.ModuleType("allday_asr.v3")
    package.paths = paths
    cli = types.ModuleType("allday_asr.cli")
    cli.app = lambda **kw: calls.append(kw["args"])
    monkeypatch.setitem(sys.modules, "allday_asr.v3", package)
    monkeypatch.setitem(sys.modules, "allday_asr.v3.paths", paths)
    monkeypatch.setitem(sys.modules, "allday_asr.cli", cli)
    monkeypatch.setattr(launcher, "inspect_source", lambda *args: {})
    monkeypatch.setattr(
        launcher, "disable_verification_jobs", lambda: guards.append(True)
    )
    monkeypatch.delenv("ALLDAY_V3_MODEL_DIR", raising=False)
    monkeypatch.delenv("ALLDAY_V3_MODEL_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    launcher.run_service(role, data, selected, cached_only)
    args = calls[0]
    assert paths.PROJECT_ROOT == data
    assert bool(guards) == cached_only
    flag = "--v3-state-dir" if role == "receiver" else "--state-dir"
    assert args[args.index(flag) + 1] == str(data / "state/v3")
    if role == "receiver":
        for flag, relative in [
            ("--inbox", "data/phone-inbox"),
            ("--tls-identity-dir", "state/transfer-tls"),
            ("--passkey-state", "state/transfer-passkeys.json"),
            ("--device-state", "state/transfer-devices.json"),
            ("--workflow-config", "allday-asr.toml"),
        ]:
            assert args[args.index(flag) + 1] == str(data / relative)
        assert ("--no-auto-process" in args) == cached_only
