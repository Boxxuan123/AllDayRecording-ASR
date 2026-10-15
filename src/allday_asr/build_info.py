"""Immutable packaged build identity; honest diagnostics for source checkouts."""
from __future__ import annotations

import json
import subprocess
import tomllib
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path


def collect_build(root: Path, *, built: bool = True) -> dict:
    project = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))["project"]

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(root), *args], timeout=5, text=True,
            stderr=subprocess.DEVNULL,
        ).strip()

    try:
        commit = git("rev-parse", "HEAD")
        dirty = bool(git("status", "--porcelain", "--untracked-files=normal"))
    except (OSError, subprocess.SubprocessError):
        commit, dirty = "unknown", None
    return {
        "component": "pc-backend", "release_version": project["version"],
        "git_commit": commit, "dirty": dirty,
        "built_at": datetime.now(timezone.utc).isoformat() if built else None,
        "identity_source": "build" if built else "source-checkout",
    }


@lru_cache(maxsize=1)
def runtime_build() -> dict:
    manifest = Path(__file__).with_name("_build_info.json")
    if manifest.exists():
        return json.loads(manifest.read_text("utf-8"))
    root = Path(__file__).resolve().parents[2]
    if (root / "pyproject.toml").exists():
        return collect_build(root, built=False)
    from importlib.metadata import version
    return {"component": "pc-backend", "release_version": version("allday-recording-asr"),
            "git_commit": "unknown", "built_at": None, "dirty": None,
            "identity_source": "historical-package"}


def diagnostics(schema_version: int | None = None) -> dict:
    from allday_asr.v3 import CONTRACT_VERSION
    from allday_asr.v3.interfaces.transfer.protocol import PROTOCOL_VERSION
    frontend_path = Path(__file__).parent / "v3/web_assets/build-info.json"
    frontend = json.loads(frontend_path.read_text("utf-8")) if frontend_path.exists() else {
        "component": "pc-ui", "identity_source": "historical_unknown"}
    return {"build": dict(runtime_build()), "frontend_build": frontend, "contract_version": CONTRACT_VERSION,
            "transfer_protocol_version": PROTOCOL_VERSION,
            "database_schema_version": schema_version}
