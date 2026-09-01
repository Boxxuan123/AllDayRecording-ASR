from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Optional

import typer

from allday_asr.application.semantic.pipeline import (
    SemanticSettings as SemanticV2E02Settings,
)
from allday_asr.config import load_config
from allday_asr.interfaces.cli.runtime import (
    build_quality_asr_runtime,
    build_quality_diarization_runtime,
)
from allday_asr.paths import DEFAULT_CONFIG_PATH, DEFAULT_DB_PATH
from allday_asr.services.quality_workflow import run_quality_workflow
from allday_asr.services.session_backup import create_session_backup
from allday_asr.storage.database import Database
from allday_asr.v3.adapters.backup import FilesystemSessionBackupAdapter
from allday_asr.v3.adapters.release_snapshot import (
    create_release_backups,
    verify_release_backup,
)
from allday_asr.v3.adapters.models_v2 import (
    ExistingV2QualityWorkflowExecutor,
    QualityWorkflowV2Adapter,
    V2SessionMaterializer,
)
from allday_asr.v3.adapters.legacy_v2.label_migration import (
    migrate_legacy_labels,
)
from allday_asr.v3.application import (
    DurableProcessingWorker,
    LegacyImportCommand,
    RecordBackupEvidenceCommand,
    SubmitProcessingCommand,
    run_identity_calibration,
    run_timeline_quality_audit,
)
from allday_asr.v3.bootstrap import V3CorePaths, compose_v3_core, start_empty_runtime
from allday_asr.v3.config import V3ConfigurationError, V3Settings
from allday_asr.v3.interfaces.desktop_server import serve_v3_desktop
from allday_asr.v3.interfaces.temporary_label_server import (
    serve_temporary_labeler,
)
from allday_asr.v3 import CONTRACT_VERSION, PROJECTION_VERSION


app = typer.Typer(
    help="V3 durable core, processing orchestration, and migration commands.",
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
            help="V3-owned state directory; never the V2 state directory.",
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


@app.command(name="label-migrate")
def label_migrate_command(
    source_database: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, resolve_path=True),
    ] = DEFAULT_DB_PATH,
    output_dir: Annotated[
        Path | None,
        typer.Option("--output-dir", file_okay=False, resolve_path=True),
    ] = None,
) -> None:
    """Normalize all auditable V2 human labels and assess V3.1 gaps."""

    _enabled_settings()
    selected_output = output_dir or (
        V3CorePaths.from_environment().state_dir / "labels"
    )
    try:
        summary = migrate_legacy_labels(
            source_database,
            output_dir=selected_output,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(
        json.dumps(
            {
                "bundle": str(summary.bundle_path.resolve()),
                "receipt": str(summary.receipt_path.resolve()),
                "report": str(summary.report_path.resolve()),
                "source_database_sha256": summary.source_database_sha256,
                "bundle_sha256": summary.bundle_sha256,
                "receipt_sha256": summary.receipt_sha256,
                "counts": summary.counts,
                "assessment": summary.assessment,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@app.command(name="label-web")
def temporary_label_web_command(
    bundle: Annotated[
        Path | None,
        typer.Option(
            "--bundle",
            exists=True,
            dir_okay=False,
            resolve_path=True,
            help="Legacy label migration bundle; newest r2 bundle is used by default.",
        ),
    ] = None,
    source_database: Annotated[
        Path,
        typer.Option(
            "--database",
            exists=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ] = DEFAULT_DB_PATH,
    state_dir: Annotated[
        Path | None,
        typer.Option("--state-dir", file_okay=False, resolve_path=True),
    ] = None,
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8774,
    open_browser: Annotated[bool, typer.Option("--open/--no-open")] = True,
) -> None:
    """Start the disposable, loopback-only V3.1 human-labeling page."""

    _enabled_settings()
    paths = V3CorePaths.from_environment()
    label_dir = paths.state_dir / "labels"
    selected_bundle = bundle or _newest_label_bundle(label_dir)
    selected_state = state_dir or (paths.state_dir / "labeling" / "v31-temporary")
    try:
        serve_temporary_labeler(
            selected_bundle,
            source_database,
            state_dir=selected_state,
            host="127.0.0.1",
            port=port,
            open_browser=open_browser,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command(name="identity-evaluate")
def identity_evaluate_command(
    bundle: Annotated[
        Path | None,
        typer.Option("--bundle", exists=True, dir_okay=False, resolve_path=True),
    ] = None,
    progress: Annotated[
        Path | None,
        typer.Option("--progress", exists=True, dir_okay=False, resolve_path=True),
    ] = None,
    source_database: Annotated[
        Path,
        typer.Option(
            "--database",
            exists=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ] = DEFAULT_DB_PATH,
    output_dir: Annotated[
        Path | None,
        typer.Option("--output-dir", file_okay=False, resolve_path=True),
    ] = None,
    device: Annotated[str, typer.Option("--device")] = "auto",
    adapt_session: Annotated[
        list[int] | None,
        typer.Option(
            "--adapt-session",
            help="Use self labels from this session to adapt the voiceprint; exclude the whole session from holdout.",
        ),
    ] = None,
    voiceprint: Annotated[
        Path | None,
        typer.Option(
            "--voiceprint",
            exists=True,
            dir_okay=False,
            resolve_path=True,
            help="Evaluate an explicit candidate voiceprint without installing it.",
        ),
    ] = None,
) -> None:
    """Score the independent self holdout and publish a V3.1 identity policy."""

    _enabled_settings()
    paths = V3CorePaths.from_environment()
    selected_bundle = bundle or _newest_label_bundle(paths.state_dir / "labels")
    selected_progress = progress or (
        paths.state_dir / "labeling" / "v31-temporary" / "progress.json"
    )
    selected_output = output_dir or (paths.state_dir / "identity")
    try:
        summary = run_identity_calibration(
            selected_bundle,
            selected_progress,
            source_database,
            output_dir=selected_output,
            device=device,
            adaptation_session_ids=frozenset(adapt_session or []),
            voiceprint_path_override=voiceprint,
        )
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(
        json.dumps(
            {
                "accepted": summary.accepted,
                "blockers": summary.blockers,
                "self_threshold": summary.self_threshold,
                "not_self_threshold": summary.not_self_threshold,
                "metrics": asdict(summary.metrics),
                "calibration": str(summary.calibration_path.resolve()),
                "policy": str(summary.policy_path.resolve()),
                "receipt": str(summary.receipt_path.resolve()),
                "candidate_voiceprint": (
                    str(summary.candidate_voiceprint_path.resolve())
                    if summary.candidate_voiceprint_path is not None
                    else None
                ),
                "calibration_sha256": summary.calibration_sha256,
                "policy_sha256": summary.policy_sha256,
                "receipt_sha256": summary.receipt_sha256,
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


@app.command(name="legacy-import")
def legacy_import_command(
    source_database: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False)
    ],
    state_dir: Annotated[
        Path | None,
        typer.Option(
            "--state-dir",
            help="V3-owned state directory; never the V2 state directory.",
        ),
    ] = None,
    source_namespace: Annotated[
        str | None, typer.Option("--source-namespace")
    ] = None,
) -> None:
    _enabled_settings()
    paths = (
        V3CorePaths.from_state_dir(state_dir)
        if state_dir is not None
        else V3CorePaths.from_environment()
    )
    core = compose_v3_core(paths)
    core.initialize()
    result = core.import_legacy_v2.execute(
        LegacyImportCommand(
            source_database=source_database,
            source_namespace=source_namespace,
        )
    )
    typer.echo(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True))


@app.command(name="release-prepare")
def release_prepare_command(
    source_database: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, resolve_path=True)
    ],
    backup_a: Annotated[
        Path, typer.Option("--backup-a", file_okay=False, resolve_path=True)
    ],
    backup_b: Annotated[
        Path, typer.Option("--backup-b", file_okay=False, resolve_path=True)
    ],
    state_dir: Annotated[
        Path, typer.Option("--state-dir", file_okay=False, resolve_path=True)
    ],
) -> None:
    """Create two immutable V2 snapshots, import one, and write a release receipt."""

    settings = _enabled_settings()
    backups = create_release_backups(source_database, (backup_a, backup_b))
    if backups[0].dataset_digest != backups[1].dataset_digest:
        raise typer.BadParameter("V2 backup dataset digests do not match")
    paths = V3CorePaths.from_state_dir(state_dir)
    core = compose_v3_core(paths)
    schema_version = core.initialize()
    result = core.import_legacy_v2.execute(
        LegacyImportCommand(source_database=backups[0].database_path)
    )
    unmapped_count = sum(int(value) for value in result.unmapped.values())
    import_clean = not result.issues and unmapped_count == 0
    production_configured = settings.deployment_mode.value == "production"
    receipt = {
        "release": "3.0.0",
        "contract_version": CONTRACT_VERSION,
        "projection_version": PROJECTION_VERSION,
        "core_schema_version": schema_version,
        "deployment_mode": settings.deployment_mode.value,
        "production_configured": production_configured,
        "dataset_digest": backups[0].dataset_digest,
        "backups": [backup.as_dict() for backup in backups],
        "legacy_import": result.as_dict(),
        "import_clean": import_clean,
        "migration_ready": import_clean,
        "automated_cutover_ready": import_clean,
        "production_release_ready": import_clean and production_configured,
    }
    receipt_path = paths.state_dir / "release" / "v3.0-release.json"
    _write_json_atomic(receipt_path, receipt)
    output = {**receipt, "receipt": str(receipt_path)}
    typer.echo(json.dumps(output, ensure_ascii=False, sort_keys=True))
    if not import_clean:
        raise typer.Exit(code=2)


@app.command(name="release-verify")
def release_verify_command(
    receipt: Annotated[
        Path, typer.Option("--receipt", exists=True, dir_okay=False, resolve_path=True)
    ],
    state_dir: Annotated[
        Path, typer.Option("--state-dir", file_okay=False, resolve_path=True)
    ],
) -> None:
    """Recompute backup evidence and verify the frozen V3 release receipt."""

    _enabled_settings()
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    if payload.get("release") != "3.0.0":
        raise typer.BadParameter("release receipt version mismatch")
    if payload.get("contract_version") != CONTRACT_VERSION:
        raise typer.BadParameter("release receipt contract version mismatch")
    if payload.get("projection_version") != PROJECTION_VERSION:
        raise typer.BadParameter("release receipt projection version mismatch")
    if payload.get("import_clean") is not True:
        raise typer.BadParameter("release receipt contains an incomplete Legacy import")
    raw_backups = payload.get("backups")
    if not isinstance(raw_backups, list) or len(raw_backups) != 2:
        raise typer.BadParameter("release receipt must contain two backups")
    backups = [
        verify_release_backup(Path(str(item["root"]))) for item in raw_backups
    ]
    if backups[0].dataset_digest != backups[1].dataset_digest:
        raise typer.BadParameter("release backup dataset digests do not match")
    if backups[0].dataset_digest != payload.get("dataset_digest"):
        raise typer.BadParameter("release receipt dataset digest mismatch")
    core = compose_v3_core(V3CorePaths.from_state_dir(state_dir))
    schema_version = core.initialize()
    if schema_version != int(payload["core_schema_version"]):
        raise typer.BadParameter("release receipt core schema version mismatch")
    typer.echo(
        json.dumps(
            {
                "verified": True,
                "release": "3.0.0",
                "dataset_digest": backups[0].dataset_digest,
                "audio_count": backups[0].audio_count,
                "audio_bytes": backups[0].audio_bytes,
                "core_schema_version": schema_version,
                "migration_ready": True,
                "production_release_ready": bool(
                    payload.get("production_release_ready", False)
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
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
    ] = "v3-v2-adapter.1",
    input_revision: Annotated[int, typer.Option("--input-revision", min=1)] = 1,
    priority: Annotated[int, typer.Option("--priority")] = 0,
    config_json: Annotated[str, typer.Option("--config-json")] = "{}",
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


@app.command(name="worker")
def worker_command(
    once: Annotated[bool, typer.Option("--once")] = False,
    poll_seconds: Annotated[float, typer.Option("--poll-seconds", min=0.1, max=30.0)] = 2.0,
    worker_id: Annotated[str | None, typer.Option("--worker-id")] = None,
    shadow: Annotated[bool, typer.Option("--shadow")] = False,
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
    v2_database_path: Annotated[Path, typer.Option("--v2-db")] = DEFAULT_DB_PATH,
    v2_backup_root: Annotated[
        Path | None, typer.Option("--v2-backup-root", file_okay=False)
    ] = None,
    v2_backup_storage_kind: Annotated[
        str, typer.Option("--v2-backup-storage-kind")
    ] = "independent_device",
    state_dir: Annotated[Path | None, typer.Option("--state-dir")] = None,
) -> None:
    core = _initialized_core(state_dir)
    if not shadow and v2_backup_root is None:
        raise typer.BadParameter(
            "production V2 adapter requires --v2-backup-root; use --shadow only for explicit experiments"
        )
    resolved = load_config(config)
    asr_runtime = build_quality_asr_runtime(resolved, requested_profile=profile)
    diarization_runtime = build_quality_diarization_runtime(
        resolved, model_path=diarization_model_path
    )
    v2_database = Database.open(v2_database_path)
    materializer = V2SessionMaterializer(
        core.database,
        core.audio_store,
        core.artifact_store,
        core.paths.state_dir / "v2-adapter-sessions",
    )

    def run_v2(v3_session_id: str, progress) -> object:
        v2_session_id = materializer.resolve(v3_session_id, v2_database)
        if v2_backup_root is not None:
            create_session_backup(
                v2_database,
                v2_session_id,
                v2_backup_root,
                storage_kind=v2_backup_storage_kind,
                restore_drill=True,
            )
        return run_quality_workflow(
            v2_database,
            None,
            session_id=v2_session_id,
            asr_settings=asr_runtime.settings,
            diarization_settings=diarization_runtime.settings,
            semantic_settings=SemanticV2E02Settings(),
            primary_factory=asr_runtime.primary_factory,
            secondary_factory=asr_runtime.secondary_factory,
            diarization_factory=diarization_runtime.backend_factory,
            admission_mode="shadow" if shadow else "production",
            progress=progress,
        )

    selected_worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
    worker = DurableProcessingWorker(
        core.processing,
        QualityWorkflowV2Adapter(
            ExistingV2QualityWorkflowExecutor(v2_database, run_v2)
        ),
        worker_id=selected_worker_id,
        config={
            "profile": profile or "auto",
            "admission_mode": "shadow" if shadow else "production",
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


def _newest_label_bundle(directory: Path) -> Path:
    candidates = sorted(
        (
            path
            for path in directory.glob("legacy-v2-labels-r2-*.json")
            if not path.name.endswith(".receipt.json")
        ),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    if not candidates:
        raise typer.BadParameter(
            "找不到 R2 标注迁移包，请先运行 allday-asr label-migrate"
        )
    return candidates[0]


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


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as target:
            target.write(encoded)
            target.flush()
            os.fsync(target.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = ["app"]
