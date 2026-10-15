"""Windows launcher bootstrap: compatible code and unchanged data roots."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


def inspect_source(source: Path, data: Path) -> dict:
    sys.path.insert(0, str(source / "src"))
    import allday_asr
    from allday_asr.v3 import CONTRACT_VERSION, PROJECTION_VERSION
    from allday_asr.v3.adapters.sqlite.migrations import MIGRATIONS

    expected = {m.version: (m.name, m.sha256) for m in MIGRATIONS}
    recorded = {}
    database = data / "state" / "v3" / "core.sqlite3"
    if database.exists():
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            if exists:
                recorded = {
                    int(version): (name, checksum)
                    for version, name, checksum in connection.execute(
                        "SELECT version, name, sha256 FROM schema_migrations"
                    )
                }
    current, supported = max(recorded, default=0), max(expected)
    if current > supported:
        raise RuntimeError(
            f"Database schema {current} is newer than code supports ({supported}). "
            "Use the updated source checkout; do not downgrade or delete the database."
        )
    mismatches = [v for v, value in recorded.items() if expected.get(v) != value]
    if mismatches:
        raise RuntimeError(f"Migration checksum/name mismatch: {mismatches}")
    return {
        "source_root": str(source),
        "package": str(Path(allday_asr.__file__).resolve()),
        "data_root": str(data),
        "database_schema": current,
        "supported_schema": supported,
        "contract_version": CONTRACT_VERSION,
        "projection_version": PROJECTION_VERSION,
    }


def preflight(data: Path, source: Path | None) -> dict:
    candidates = (
        [source] if source else [data, data / ".task-worktrees" / "daily-ui-round1"]
    )
    errors = []
    for candidate in candidates:
        if not (candidate / "src" / "allday_asr").is_dir():
            errors.append(f"{candidate}: source not found")
            continue
        child = subprocess.run(
            [
                sys.executable,
                "-B",
                str(Path(__file__).resolve()),
                "inspect",
                "--data-root",
                str(data),
                "--source-root",
                str(candidate),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
            timeout=30,
        )
        if child.returncode == 0:
            return json.loads(child.stdout)
        errors.append(f"{candidate}: {child.stderr.strip()}")
    raise RuntimeError("No compatible source checkout.\n" + "\n".join(errors))


def disable_verification_jobs() -> None:
    """Process-local verification; never persist changed model preferences."""
    os.environ["ALLDAY_V3_CODEX_ENABLED"] = "0"
    from allday_asr.v3.adapters.blind_validation import BlindValidationService
    from allday_asr.v3.application.annotation_samples import AnnotationSampleWorker
    from allday_asr.v3.application.daily_events import DailyEventMixin
    from allday_asr.v3.application.unified_review import UnifiedReviewService

    BlindValidationService.start = lambda self: None
    AnnotationSampleWorker.start = lambda self: None
    DailyEventMixin.refresh_daily_cache = lambda self, force=False: None
    UnifiedReviewService.refresh_saved = lambda self: None


def run_service(role: str, data: Path, source: Path, cached_only: bool) -> None:
    inspect_source(source, data)
    os.chdir(data)
    os.environ.setdefault("ALLDAY_V3_MODEL_DIR", str(data / "models"))
    os.environ.setdefault("ALLDAY_V3_MODEL_CONFIG", str(data / "allday-asr.toml"))
    from allday_asr.v3 import paths

    paths.PROJECT_ROOT = data
    if cached_only:
        disable_verification_jobs()
    from allday_asr.cli import app

    if role == "receiver":
        arguments = [
            "device",
            "receive",
            "--port",
            "8766",
            "--inbox",
            str(data / "data/phone-inbox"),
            "--tls-identity-dir",
            str(data / "state/transfer-tls"),
            "--passkey-state",
            str(data / "state/transfer-passkeys.json"),
            "--device-state",
            str(data / "state/transfer-devices.json"),
            "--v3-state-dir",
            str(data / "state/v3"),
            "--workflow-config",
            str(data / "allday-asr.toml"),
        ]
        if cached_only:
            arguments.append("--no-auto-process")
    else:
        arguments = [
            "web",
            "--port",
            "8765",
            "--no-open",
            "--state-dir",
            str(data / "state/v3"),
        ]
    app(args=arguments)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("preflight", "inspect", "run"))
    parser.add_argument("--data-root", type=lambda s: Path(s).resolve(), required=True)
    parser.add_argument("--source-root", type=lambda s: Path(s).resolve())
    parser.add_argument("--role", choices=("receiver", "web"))
    parser.add_argument("--cached-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "preflight":
            print(json.dumps(preflight(args.data_root, args.source_root)))
        elif args.action == "inspect":
            print(json.dumps(inspect_source(args.source_root, args.data_root)))
        else:
            if args.role is None or args.source_root is None:
                parser.error("run requires --role and --source-root")
            run_service(args.role, args.data_root, args.source_root, args.cached_only)
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Startup preflight failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
