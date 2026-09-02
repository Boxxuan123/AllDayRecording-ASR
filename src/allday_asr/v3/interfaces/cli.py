from __future__ import annotations

import json
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Optional

import typer

from allday_asr.v3.model_config import load_model_config
from allday_asr.v3.paths import DEFAULT_CONFIG_PATH
from allday_asr.v3.adapters.backup import FilesystemSessionBackupAdapter
from allday_asr.v3.adapters.models import build_native_model_pipeline
from allday_asr.v3.application import (
    DurableProcessingWorker,
    RecordBackupEvidenceCommand,
    SubmitProcessingCommand,
    run_timeline_quality_audit,
)
from allday_asr.v3.bootstrap import V3CorePaths, compose_v3_core, start_empty_runtime
from allday_asr.v3.config import V3ConfigurationError, V3Settings
from allday_asr.v3.interfaces.desktop_server import serve_v3_desktop


app = typer.Typer(
    help="V3 durable core and processing orchestration commands.",
    no_args_is_help=True,
)


@app.command(name="status")
def status_command() -> None:
    settings = _settings()
    typer.echo(
        json.dumps(
            {
                "enabled": settings.enabled,
                "deployment_mode": settings.deployment_mode.value,
                "state": "disabled" if not settings.enabled else "startable",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@app.command(name="start")
def start_command() -> None:
    settings = _settings()
    try:
        runtime = start_empty_runtime(settings)
    except V3ConfigurationError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(runtime.as_dict(), ensure_ascii=False, sort_keys=True))


@app.command(name="migrate")
def migrate_command(
    state_dir: Annotated[
        Path | None,
        typer.Option(
            "--state-dir",
            help="V3-owned state directory.",
        ),
    ] = None,
) -> None:
    _enabled_settings()
    paths = (
        V3CorePaths.from_state_dir(state_dir)
        if state_dir is not None
        else V3CorePaths.from_environment()
    )
    core = compose_v3_core(paths)
    version = core.initialize()
    typer.echo(
        json.dumps(
            {
                "database": str(paths.database_path),
                "schema_version": version,
                "state": "ready",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@app.command(name="seam-audit")
def timeline_audit_command(
    source: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, resolve_path=True),
    ],
    output: Annotated[
        Path | None,
        typer.Option("--output", dir_okay=False, resolve_path=True),
    ] = None,
) -> None:
    """Evaluate reviewed long-recording chunk seams and write a hashed receipt."""

    _enabled_settings()
    try:
        summary = run_timeline_quality_audit(source, receipt_path=output)
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(
        json.dumps(
            {
                "accepted": summary.accepted,
                "blockers": summary.blockers,
                "input_sha256": summary.input_sha256,
                "receipt_sha256": summary.receipt_sha256,
                "receipt": str(summary.receipt_path.resolve()),
                "metrics": summary.metrics,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    if not summary.accepted:
        raise typer.Exit(code=2)


@app.command(name="desktop")
def desktop_command(
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8766,
    open_browser: Annotated[bool, typer.Option("--open/--no-open")] = True,
    state_dir: Annotated[Path | None, typer.Option("--state-dir")] = None,
) -> None:
    """Start the loopback-only V3 Desktop API and independent frontend."""
    _enabled_settings()
    paths = (
        V3CorePaths.from_state_dir(state_dir)
        if state_dir is not None
        else V3CorePaths.from_environment()
    )
    serve_v3_desktop(
        paths=paths,
        host="127.0.0.1",
        port=port,
        open_browser=open_browser,
    )


@app.command(name="web")
def web_command(
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8765,
    open_browser: Annotated[bool, typer.Option("--open/--no-open")] = True,
    state_dir: Annotated[Path | None, typer.Option("--state-dir")] = None,
) -> None:
    """Start the default V3 Desktop entry on the traditional web port."""

    _enabled_settings()
    paths = (
        V3CorePaths.from_state_dir(state_dir)
        if state_dir is not None
        else V3CorePaths.from_environment()
    )
    serve_v3_desktop(
        paths=paths,
        host="127.0.0.1",
        port=port,
        open_browser=open_browser,
    )


@app.command(name="backup-admit")
def backup_admit_command(
    session_id: Annotated[str, typer.Argument(help="V3 recording session ID.")],
    backup_root: Annotated[
        Path,
        typer.Option(
            "--backup-root",
            file_okay=False,
            help="Independent device or network backup root.",
        ),
    ],
    storage_kind: Annotated[
        str,
        typer.Option(
            "--storage-kind", help="independent_device or network."
        ),
    ] = "independent_device",
    state_dir: Annotated[Path | None, typer.Option("--state-dir")] = None,
) -> None:
    core = _initialized_core(state_dir)
    result = FilesystemSessionBackupAdapter(
        core.database, core.audio_store, core.artifact_store
    ).backup(session_id, backup_root, storage_kind=storage_kind)
    admitted = core.admission.record_verified_backup(
        RecordBackupEvidenceCommand(
            session_id=session_id,
            provider=result.provider,
            storage_kind=result.storage_kind,
            digest=result.digest,
            restore_checked_at=datetime.now(timezone.utc),
            metadata={
                "file_count": result.file_count,
                "byte_count": result.byte_count,
                "restore_drill": True,
            },
        )
    )
    typer.echo(
        json.dumps(
            {
                "session_id": session_id,
                "admitted": admitted,
                "backup_digest": result.digest,
                "file_count": result.file_count,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@app.command(name="process-submit")
def process_submit_command(
    session_id: Annotated[str, typer.Argument(help="Admitted V3 session ID.")],
    pipeline_version: Annotated[
        str, typer.Option("--pipeline-version")
    ] = "v3-native.1",
    input_revision: Annotated[int, typer.Option("--input-revision", min=1)] = 1,
    priority: Annotated[int, typer.Option("--priority")] = 0,
    config_json: Annotated[str, typer.Option("--config-json")] = "{}",
    force_reprocess: Annotated[
        bool,
        typer.Option(
            "--force-reprocess/--reuse-succeeded",
            help="Create a new run even when an identical successful run exists.",
        ),
    ] = False,
    state_dir: Annotated[Path | None, typer.Option("--state-dir")] = None,
) -> None:
    core = _initialized_core(state_dir)
    config = _json_object(config_json, "--config-json")
    snapshot = core.processing.submit(
        SubmitProcessingCommand(
            session_id=session_id,
            pipeline_version=pipeline_version,
            input_revision=input_revision,
            config=config,
            priority=priority,
            force_reprocess=force_reprocess,
        )
    )
    typer.echo(json.dumps(_snapshot_dict(snapshot), ensure_ascii=False, sort_keys=True))


@app.command(name="process-status")
def process_status_command(
    job_id: Annotated[str, typer.Argument(help="Durable processing job ID.")],
    state_dir: Annotated[Path | None, typer.Option("--state-dir")] = None,
) -> None:
    snapshot = _initialized_core(state_dir).processing.get(job_id)
    typer.echo(json.dumps(_snapshot_dict(snapshot), ensure_ascii=False, sort_keys=True))


@app.command(name="process-retry")
def process_retry_command(
    job_id: Annotated[str, typer.Argument()],
    state_dir: Annotated[Path | None, typer.Option("--state-dir")] = None,
) -> None:
    snapshot = _initialized_core(state_dir).processing.retry(job_id)
    typer.echo(json.dumps(_snapshot_dict(snapshot), ensure_ascii=False, sort_keys=True))


@app.command(name="process-cancel")
def process_cancel_command(
    job_id: Annotated[str, typer.Argument()],
    reason: Annotated[str, typer.Option("--reason")] = "cancelled by user",
    state_dir: Annotated[Path | None, typer.Option("--state-dir")] = None,
) -> None:
    snapshot = _initialized_core(state_dir).processing.cancel(job_id, reason)
    typer.echo(json.dumps(_snapshot_dict(snapshot), ensure_ascii=False, sort_keys=True))


@app.command(name="process-recover")
def process_recover_command(
    state_dir: Annotated[Path | None, typer.Option("--state-dir")] = None,
) -> None:
    recovered = _initialized_core(state_dir).processing.recover_expired()
    typer.echo(json.dumps({"recovered_job_ids": recovered}, ensure_ascii=False))


@app.command(name="semantic-events-generate")
def semantic_events_generate_command(
    session_id: Annotated[
        str, typer.Argument(help="V3 recording session ID with active utterances.")
    ],
    reasoning_effort: Annotated[
        str,
        typer.Option(
            "--reasoning-effort",
            help="auto, low, medium, high or xhigh.",
        ),
    ] = "auto",
    state_dir: Annotated[Path | None, typer.Option("--state-dir")] = None,
) -> None:
    """Generate evidence-bound native events for one existing V3 session."""

    core = _initialized_core(state_dir)
    try:
        result = core.semantic_events.extract(
            session_id,
            reasoning_effort=reasoning_effort,
        )
    except (KeyError, RuntimeError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    finally:
        core.close()
    typer.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))


@app.command(name="worker")
def worker_command(
    once: Annotated[bool, typer.Option("--once")] = False,
    poll_seconds: Annotated[float, typer.Option("--poll-seconds", min=0.1, max=30.0)] = 2.0,
    worker_id: Annotated[str | None, typer.Option("--worker-id")] = None,
    profile: Annotated[Optional[str], typer.Option()] = None,
    diarization_model_path: Annotated[
        Optional[Path],
        typer.Option(
            "--diarization-model-path",
            exists=True,
            file_okay=False,
            resolve_path=True,
        ),
    ] = None,
    config: Annotated[
        Path,
        typer.Option(exists=True, file_okay=True, resolve_path=True),
    ] = DEFAULT_CONFIG_PATH,
    state_dir: Annotated[Path | None, typer.Option("--state-dir")] = None,
) -> None:
    core = _initialized_core(state_dir)
    resolved = load_model_config(config)
    adapter = build_native_model_pipeline(
        core.database,
        core.audio_store,
        resolved,
        requested_profile=profile,
        diarization_model_path=diarization_model_path,
    )

    selected_worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
    worker = DurableProcessingWorker(
        core.processing,
        adapter,
        worker_id=selected_worker_id,
        config={
            "profile": profile or "auto",
            "pipeline": "v3-native.1",
        },
    )
    while True:
        worked = worker.run_once()
        if once:
            typer.echo(
                json.dumps(
                    {"worker_id": selected_worker_id, "worked": worked},
                    ensure_ascii=False,
                )
            )
            return
        if not worked:
            time.sleep(poll_seconds)


def _settings() -> V3Settings:
    try:
        return V3Settings.from_environment()
    except V3ConfigurationError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _enabled_settings() -> V3Settings:
    settings = _settings()
    if not settings.enabled:
        raise typer.BadParameter(
            "V3 is disabled; set ALLDAY_V3_ENABLED=1 before modifying V3 state"
        )
    return settings


def _initialized_core(state_dir: Path | None):
    _enabled_settings()
    paths = (
        V3CorePaths.from_state_dir(state_dir)
        if state_dir is not None
        else V3CorePaths.from_environment()
    )
    core = compose_v3_core(paths)
    core.initialize()
    return core


def _json_object(value: str, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"{label} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise typer.BadParameter(f"{label} must be a JSON object")
    return parsed


def _snapshot_dict(snapshot) -> dict[str, Any]:
    return {
        "job_id": snapshot.job.job_id,
        "run_id": snapshot.run.run_id,
        "session_id": snapshot.run.session_id,
        "pipeline_version": snapshot.run.pipeline_version,
        "status": snapshot.job.status,
        "current_stage": snapshot.run.current_stage,
        "progress": snapshot.run.progress,
        "error": snapshot.job.error,
        "stages": [
            {
                "stage": stage.stage,
                "status": stage.status.value,
                "optional": stage.optional,
                "attempt_count": sum(
                    attempt.stage_run_id == stage.stage_run_id
                    for attempt in snapshot.attempts
                ),
                "error": stage.error,
            }
            for stage in snapshot.stages
        ],
    }


__all__ = ["app"]
