from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from allday_asr.application.use_cases.session_integrity import verify_session_inputs
from allday_asr.audio.tools import sha256_file
from allday_asr.domain.hashing import canonical_json_sha256 as _sha256_json
from allday_asr.storage.database import Database


BACKUP_FORMAT = "AllDayRecording immutable session backup v1"
BACKUP_DIRECTORY = "allday-backup-v1"
PRODUCTION_STORAGE_KINDS = frozenset({"independent_device", "network"})
STORAGE_KINDS = frozenset({*PRODUCTION_STORAGE_KINDS, "same_device_test"})


@dataclass(frozen=True)
class SessionBackupSummary:
    backup_id: int
    session_id: int
    backup_path: Path
    storage_kind: str
    file_count: int
    total_bytes: int
    created: bool
    restore_verified: bool


@dataclass(frozen=True)
class SessionBackupVerificationSummary:
    backup_id: int
    session_id: int
    file_count: int
    total_bytes: int
    restore_drill: bool
    production_grade: bool


def create_session_backup(
    database: Database,
    session_id: int,
    destination_root: Path,
    *,
    storage_kind: str,
    restore_drill: bool = True,
    restore_probe_root: Path | None = None,
) -> SessionBackupSummary:
    """Copy each immutable original once, verify it, and persist backup evidence."""
    if storage_kind not in STORAGE_KINDS:
        raise ValueError(
            "storage_kind 必须是 independent_device、network 或 same_device_test"
        )
    integrity = verify_session_inputs(database, session_id)
    failures = [
        item for item in integrity["instances"] if item["status"] != "verified"
    ]
    if integrity["manifest"]["status"] not in {"verified", "not_applicable"}:
        failures.append(integrity["manifest"])
    if failures:
        raise RuntimeError(
            f"原始输入完整性校验失败，共 {len(failures)} 项；不会创建备份"
        )

    session = database.get_recording_session(session_id)
    sources = database.list_session_sources(session_id)
    destination_root = destination_root.resolve()
    if destination_root.exists() and not destination_root.is_dir():
        raise ValueError("备份目标必须是目录")
    destination_root.mkdir(parents=True, exist_ok=True)
    destination_root = destination_root.resolve(strict=True)
    manifest = database.get_session_manifest(session_id)
    protected_inputs = [Path(str(row["source_path"])) for row in sources]
    if manifest is not None:
        protected_inputs.append(Path(str(manifest["manifest_path"])))
    _reject_mixed_source_and_destination(destination_root, protected_inputs)

    input_fingerprint = database.session_input_fingerprint(session_id)
    session_hash = hashlib.sha256(
        str(session["session_key"]).encode("utf-8")
    ).hexdigest()
    final_directory = (
        destination_root
        / BACKUP_DIRECTORY
        / f"s-{session_hash[:32]}"
        / f"i-{input_fingerprint[:32]}"
    )
    planned = _planned_files(database, session_id, sources)
    ledger = _backup_ledger(
        session,
        input_fingerprint=input_fingerprint,
        storage_kind=storage_kind,
        files=planned,
    )
    ledger_bytes = (
        json.dumps(ledger, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    ledger_sha256 = hashlib.sha256(ledger_bytes).hexdigest()
    backup_key = _sha256_json(
        {
            "session_key": str(session["session_key"]),
            "input_fingerprint": input_fingerprint,
            "storage_kind": storage_kind,
            "backup_path": str(final_directory),
            "backup_manifest_sha256": ledger_sha256,
        }
    )
    existing_record = next(
        (
            row
            for row in database.list_session_backups(session_id)
            if str(row["backup_key"]) == backup_key
        ),
        None,
    )

    if final_directory.exists():
        _verify_directory(final_directory, planned, ledger_sha256=ledger_sha256)
    else:
        _copy_backup_atomically(
            final_directory,
            planned,
            ledger_bytes=ledger_bytes,
            ledger_sha256=ledger_sha256,
        )

    file_evidence = [
        {
            "position": item["position"],
            "file_kind": item["file_kind"],
            "source_instance_id": item.get("source_instance_id"),
            "relative_path": item["relative_path"],
            "sha256": item["sha256"],
            "byte_size": item["byte_size"],
        }
        for item in planned
    ]
    backup = database.create_verified_session_backup(
        {
            "backup_key": backup_key,
            "session_id": session_id,
            "storage_kind": storage_kind,
            "backup_path": str(final_directory),
            "input_fingerprint": input_fingerprint,
            "backup_manifest_sha256": ledger_sha256,
        },
        file_evidence,
    )
    if restore_drill:
        verify_session_backup(
            database,
            int(backup["id"]),
            restore_drill=True,
            restore_probe_root=restore_probe_root,
        )
        backup = database.get_session_backup(int(backup["id"]))
    return SessionBackupSummary(
        backup_id=int(backup["id"]),
        session_id=session_id,
        backup_path=final_directory,
        storage_kind=storage_kind,
        file_count=len(planned),
        total_bytes=sum(int(item["byte_size"]) for item in planned),
        created=existing_record is None,
        restore_verified=backup["restore_verified_at"] is not None,
    )


def verify_session_backup(
    database: Database,
    backup_id: int,
    *,
    restore_drill: bool = False,
    restore_probe_root: Path | None = None,
) -> SessionBackupVerificationSummary:
    backup = database.get_session_backup(backup_id)
    root = Path(str(backup["backup_path"]))
    files = database.list_session_backup_files(backup_id)
    planned = [
        {
            "position": int(row["position"]),
            "file_kind": str(row["file_kind"]),
            "source_instance_id": row["source_instance_id"],
            "relative_path": str(row["relative_path"]),
            "sha256": str(row["sha256"]),
            "byte_size": int(row["byte_size"]),
        }
        for row in files
    ]
    try:
        _verify_directory(
            root,
            planned,
            ledger_sha256=str(backup["backup_manifest_sha256"]),
        )
        database.create_verified_session_backup(
            {
                "backup_key": str(backup["backup_key"]),
                "session_id": int(backup["session_id"]),
                "storage_kind": str(backup["storage_kind"]),
                "backup_path": str(root),
                "input_fingerprint": str(backup["input_fingerprint"]),
                "backup_manifest_sha256": str(backup["backup_manifest_sha256"]),
            },
            planned,
        )
        if restore_drill:
            _run_restore_drill(
                root,
                planned,
                ledger_sha256=str(backup["backup_manifest_sha256"]),
                restore_probe_root=restore_probe_root,
            )
            database.mark_session_backup_restore_verified(backup_id)
    except Exception as exc:
        database.mark_session_backup_failed(backup_id, repr(exc))
        raise
    return SessionBackupVerificationSummary(
        backup_id=backup_id,
        session_id=int(backup["session_id"]),
        file_count=len(planned),
        total_bytes=sum(int(item["byte_size"]) for item in planned),
        restore_drill=restore_drill,
        production_grade=(
            str(backup["storage_kind"]) in PRODUCTION_STORAGE_KINDS
            and (
                restore_drill
                or database.get_session_backup(backup_id)["restore_verified_at"]
                is not None
            )
        ),
    )


def _planned_files(
    database: Database, session_id: int, sources: list[Any]
) -> list[dict[str, Any]]:
    planned: list[dict[str, Any]] = []
    for position, row in enumerate(sources):
        source_path = Path(str(row["source_path"])).resolve(strict=True)
        suffix = source_path.suffix.lower()
        relative_path = (
            f"audio/{position:06d}-{int(row['source_instance_id']):08d}-"
            f"{str(row['sha256'])[:16]}{suffix}"
        )
        planned.append(
            {
                "position": position,
                "file_kind": "source_audio",
                "source_instance_id": int(row["source_instance_id"]),
                "instance_key": str(row["instance_key"]),
                "original_filename": str(row["original_filename"]),
                "source_path": source_path,
                "relative_path": relative_path,
                "sha256": str(row["sha256"]),
                "byte_size": int(row["instance_byte_size"]),
                "chunk_index": int(row["chunk_index"]),
                "session_start_ms": int(row["session_start_ms"]),
                "session_end_ms": int(row["session_end_ms"]),
                "session_start_sample": row["session_start_sample"],
                "session_end_sample": row["session_end_sample"],
                "timeline_sample_rate": row["timeline_sample_rate"],
            }
        )
    manifest = database.get_session_manifest(session_id)
    if manifest is not None:
        manifest_path = Path(str(manifest["manifest_path"])).resolve(strict=True)
        planned.append(
            {
                "position": len(planned),
                "file_kind": "capture_manifest",
                "source_instance_id": None,
                "original_filename": manifest_path.name,
                "source_path": manifest_path,
                "relative_path": (
                    f"capture/manifest-{str(manifest['manifest_sha256'])[:16]}.json"
                ),
                "sha256": str(manifest["manifest_sha256"]),
                "byte_size": int(manifest["byte_size"]),
            }
        )
    return planned


def _backup_ledger(
    session: Any,
    *,
    input_fingerprint: str,
    storage_kind: str,
    files: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "format": BACKUP_FORMAT,
        "session": {
            "session_key": str(session["session_key"]),
            "recorded_at": str(session["recorded_at"]),
            "timezone": str(session["timezone"]),
            "duration_ms": int(session["duration_ms"]),
            "input_fingerprint": input_fingerprint,
        },
        "storage_kind": storage_kind,
        "files": [
            {
                key: item[key]
                for key in (
                    "position",
                    "file_kind",
                    "source_instance_id",
                    "original_filename",
                    "relative_path",
                    "sha256",
                    "byte_size",
                    "instance_key",
                    "chunk_index",
                    "session_start_ms",
                    "session_end_ms",
                    "session_start_sample",
                    "session_end_sample",
                    "timeline_sample_rate",
                )
                if key in item
            }
            for item in files
        ],
    }


def _copy_backup_atomically(
    final_directory: Path,
    planned: list[dict[str, Any]],
    *,
    ledger_bytes: bytes,
    ledger_sha256: str,
) -> None:
    final_directory.parent.mkdir(parents=True, exist_ok=True)
    staging = final_directory.parent / f".staging-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        for item in planned:
            destination = _safe_backup_path(staging, str(item["relative_path"]))
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item["source_path"], destination)
            _verify_file(
                destination,
                expected_sha256=str(item["sha256"]),
                expected_size=int(item["byte_size"]),
            )
        ledger_path = staging / "backup-manifest.json"
        ledger_path.write_bytes(ledger_bytes)
        _verify_file(
            ledger_path,
            expected_sha256=ledger_sha256,
            expected_size=len(ledger_bytes),
        )
        staging.replace(final_directory)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _verify_directory(
    root: Path,
    planned: list[dict[str, Any]],
    *,
    ledger_sha256: str,
) -> None:
    if root.is_symlink():
        raise RuntimeError("备份根目录不能是符号链接")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise RuntimeError("备份路径不是目录")
    ledger_path = _safe_backup_path(root, "backup-manifest.json")
    _verify_file(ledger_path, expected_sha256=ledger_sha256)
    expected_paths = {"backup-manifest.json"}
    for item in planned:
        relative_path = str(item["relative_path"])
        expected_paths.add(Path(relative_path).as_posix())
        path = _safe_backup_path(root, relative_path)
        _verify_file(
            path,
            expected_sha256=str(item["sha256"]),
            expected_size=int(item["byte_size"]),
        )
    expected_entries = set(expected_paths)
    for relative_path in expected_paths:
        expected_entries.update(
            parent.as_posix()
            for parent in Path(relative_path).parents
            if parent != Path(".")
        )
    actual_entries = {
        path.relative_to(root).as_posix() for path in root.rglob("*")
    }
    unexpected = sorted(actual_entries - expected_entries)
    if unexpected:
        raise RuntimeError(f"备份目录包含清单外文件：{unexpected[0]}")


def _run_restore_drill(
    backup_root: Path,
    planned: list[dict[str, Any]],
    *,
    ledger_sha256: str,
    restore_probe_root: Path | None,
) -> None:
    base = (
        restore_probe_root.resolve()
        if restore_probe_root is not None
        else Path(tempfile.gettempdir()).resolve()
    )
    base.mkdir(parents=True, exist_ok=True)
    base = base.resolve(strict=True)
    probe = base / f"allday-asr-restore-probe-{uuid.uuid4().hex}"
    probe.mkdir()
    try:
        ledger_source = _safe_backup_path(backup_root, "backup-manifest.json")
        ledger_destination = probe / "backup-manifest.json"
        shutil.copyfile(ledger_source, ledger_destination)
        _verify_file(ledger_destination, expected_sha256=ledger_sha256)
        for item in planned:
            source = _safe_backup_path(backup_root, str(item["relative_path"]))
            destination = _safe_backup_path(probe, str(item["relative_path"]))
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            _verify_file(
                destination,
                expected_sha256=str(item["sha256"]),
                expected_size=int(item["byte_size"]),
            )
    finally:
        resolved_probe = probe.resolve()
        try:
            resolved_probe.relative_to(base)
        except ValueError as exc:
            raise RuntimeError("恢复演练目录越出指定根目录") from exc
        if resolved_probe.exists():
            shutil.rmtree(resolved_probe)


def _verify_file(
    path: Path, *, expected_sha256: str, expected_size: int | None = None
) -> None:
    if path.is_symlink():
        raise RuntimeError(f"备份文件不能是符号链接：{path.name}")
    if not path.is_file():
        raise RuntimeError(f"备份文件不存在：{path}")
    actual_size = path.stat().st_size
    if expected_size is not None and actual_size != expected_size:
        raise RuntimeError(
            f"备份文件大小不一致：{path.name} expected={expected_size} actual={actual_size}"
        )
    actual_sha256 = sha256_file(path)
    if actual_sha256 != expected_sha256:
        raise RuntimeError(f"备份文件 SHA-256 不一致：{path.name}")


def _safe_backup_path(root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError("备份清单包含不安全的相对路径")
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise RuntimeError("备份路径不能经过符号链接")
    candidate = cursor.resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeError("备份文件路径越出备份目录") from exc
    return candidate


def _reject_mixed_source_and_destination(
    destination_root: Path, protected_inputs: list[Path]
) -> None:
    for input_path in protected_inputs:
        source = input_path.resolve(strict=True)
        try:
            destination_root.relative_to(source.parent)
        except ValueError:
            pass
        else:
            raise ValueError("备份目标不能位于任一原音所在目录内")
        try:
            source.relative_to(destination_root)
        except ValueError:
            pass
        else:
            raise ValueError("原音不能位于备份目标目录内")
