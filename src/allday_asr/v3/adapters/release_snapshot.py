from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


_MANIFEST_NAME = "v3.0-v2-backup.json"
_DATABASE_NAME = "v2.sqlite3"


@dataclass(frozen=True)
class ReleaseBackup:
    root: Path
    manifest_path: Path
    database_path: Path
    source_database_sha256: str
    database_sha256: str
    dataset_digest: str
    source_schema_version: int
    audio_count: int
    audio_bytes: int

    def as_dict(self) -> dict[str, str | int]:
        return {
            "root": str(self.root),
            "manifest": str(self.manifest_path),
            "database": str(self.database_path),
            "source_database_sha256": self.source_database_sha256,
            "database_sha256": self.database_sha256,
            "dataset_digest": self.dataset_digest,
            "source_schema_version": self.source_schema_version,
            "audio_count": self.audio_count,
            "audio_bytes": self.audio_bytes,
        }


def create_release_backups(
    source_database: Path,
    destinations: Sequence[Path],
    *,
    now: Callable[[], datetime] | None = None,
) -> tuple[ReleaseBackup, ReleaseBackup]:
    """Create or verify exactly two immutable V2 database/audio snapshots."""

    if len(destinations) != 2:
        raise ValueError("V3.0 release requires exactly two backup destinations")
    source = source_database.resolve(strict=True)
    roots = tuple(destination.resolve() for destination in destinations)
    if roots[0] == roots[1]:
        raise ValueError("the two backup destinations must be different")
    if roots[0] in roots[1].parents or roots[1] in roots[0].parents:
        raise ValueError("release backup destinations cannot contain each other")
    for root in roots:
        if source == root or root in source.parents:
            raise ValueError("a backup destination cannot contain the source database")

    created_at = (now or (lambda: datetime.now(timezone.utc)))()
    source_fingerprint = _database_fingerprint(source)
    connection = _open_read_only(source)
    try:
        connection.execute("BEGIN")
        schema_version = _schema_version(connection)
        inventory = _audio_inventory(connection, source)
        dataset_digest = _dataset_digest(
            source_fingerprint,
            schema_version,
            inventory,
        )
        backups = tuple(
            _create_or_verify(
                root,
                connection=connection,
                source_database=source,
                source_fingerprint=source_fingerprint,
                schema_version=schema_version,
                inventory=inventory,
                dataset_digest=dataset_digest,
                created_at=created_at,
            )
            for root in roots
        )
        _verify_source_audio(inventory, source)
    finally:
        connection.close()

    if _database_fingerprint(source) != source_fingerprint:
        raise RuntimeError("V2 database changed while release backups were created")
    first, second = backups
    if first.dataset_digest != second.dataset_digest:
        raise RuntimeError("the two release backups describe different datasets")
    return first, second


def verify_release_backup(root: Path) -> ReleaseBackup:
    resolved = root.resolve(strict=True)
    manifest_path = resolved / _MANIFEST_NAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("format_version") != 1:
        raise ValueError("release backup manifest version is unsupported")
    database_path = resolved / str(payload.get("database", ""))
    if not database_path.is_relative_to(resolved) or not database_path.is_file():
        raise ValueError("release backup database path is invalid")
    database_sha256 = _file_sha256(database_path)
    if database_sha256 != payload.get("database_sha256"):
        raise ValueError("release backup database SHA-256 mismatch")
    _verify_database(database_path)

    entries = payload.get("audio")
    if not isinstance(entries, list):
        raise ValueError("release backup audio inventory is invalid")
    audio_bytes = 0
    normalized: list[dict[str, Any]] = []
    for raw in entries:
        if not isinstance(raw, dict):
            raise ValueError("release backup audio entry is invalid")
        relative = Path(str(raw.get("backup_path", "")))
        target = (resolved / relative).resolve()
        if not target.is_relative_to(resolved) or not target.is_file():
            raise ValueError("release backup audio path is invalid")
        expected_size = int(raw["size_bytes"])
        expected_sha256 = str(raw["sha256"])
        if target.stat().st_size != expected_size:
            raise ValueError(f"release backup audio size mismatch: {relative}")
        if _file_sha256(target) != expected_sha256:
            raise ValueError(f"release backup audio SHA-256 mismatch: {relative}")
        _require_read_only(target)
        audio_bytes += expected_size
        normalized.append(raw)
    _require_read_only(database_path)
    _require_read_only(manifest_path)

    source_fingerprint = str(payload["source_database_sha256"])
    schema_version = int(payload["source_schema_version"])
    dataset_digest = _dataset_digest(
        source_fingerprint,
        schema_version,
        normalized,
    )
    if dataset_digest != payload.get("dataset_digest"):
        raise ValueError("release backup dataset digest mismatch")
    return ReleaseBackup(
        root=resolved,
        manifest_path=manifest_path,
        database_path=database_path,
        source_database_sha256=source_fingerprint,
        database_sha256=database_sha256,
        dataset_digest=dataset_digest,
        source_schema_version=schema_version,
        audio_count=len(normalized),
        audio_bytes=audio_bytes,
    )


def _create_or_verify(
    root: Path,
    *,
    connection: sqlite3.Connection,
    source_database: Path,
    source_fingerprint: str,
    schema_version: int,
    inventory: list[dict[str, Any]],
    dataset_digest: str,
    created_at: datetime,
) -> ReleaseBackup:
    if root.exists():
        current = verify_release_backup(root)
        if current.dataset_digest != dataset_digest:
            raise ValueError(f"existing backup does not match current V2 data: {root}")
        return current

    root.parent.mkdir(parents=True, exist_ok=True)
    partial = root.parent / f".{root.name}.partial-{uuid4().hex}"
    partial.mkdir()
    try:
        database_path = partial / _DATABASE_NAME
        destination = sqlite3.connect(database_path)
        try:
            connection.backup(destination)
        finally:
            destination.close()
        _verify_database(database_path)
        database_sha256 = _file_sha256(database_path)

        copied: list[dict[str, Any]] = []
        for entry in inventory:
            source_path = Path(str(entry["source_path"]))
            if not source_path.is_absolute():
                source_path = source_database.parent / source_path
            source_path = source_path.resolve(strict=True)
            expected_size = int(entry["size_bytes"])
            expected_sha256 = str(entry["sha256"])
            if source_path.stat().st_size != expected_size:
                raise ValueError(f"V2 audio size mismatch: {source_path}")
            if _file_sha256(source_path) != expected_sha256:
                raise ValueError(f"V2 audio SHA-256 mismatch: {source_path}")
            safe_name = _safe_filename(source_path.name)
            relative = Path("audio") / f"{int(entry['instance_id']):06d}-{safe_name}"
            target = partial / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, target)
            if target.stat().st_size != expected_size:
                raise RuntimeError(f"copied audio size mismatch: {target}")
            if _file_sha256(target) != expected_sha256:
                raise RuntimeError(f"copied audio SHA-256 mismatch: {target}")
            target.chmod(0o444)
            copied.append({**entry, "backup_path": relative.as_posix()})

        database_path.chmod(0o444)
        manifest = {
            "format_version": 1,
            "release": "3.0.0",
            "created_at": created_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "source_database": str(source_database),
            "source_database_sha256": source_fingerprint,
            "source_schema_version": schema_version,
            "database": _DATABASE_NAME,
            "database_sha256": database_sha256,
            "dataset_digest": dataset_digest,
            "audio": copied,
        }
        manifest_path = partial / _MANIFEST_NAME
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        manifest_path.chmod(0o444)
        partial.replace(root)
    except Exception:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return verify_release_backup(root)


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=ro",
        uri=True,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)


def _audio_inventory(
    connection: sqlite3.Connection,
    source_database: Path,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT
            si.id AS instance_id,
            si.instance_key,
            si.source_path,
            si.byte_size AS size_bytes,
            so.sha256
        FROM source_instances si
        JOIN source_objects so ON so.id = si.source_object_id
        ORDER BY si.id
        """
    )
    inventory: list[dict[str, Any]] = []
    for row in rows:
        path = Path(str(row["source_path"]))
        if not path.is_absolute():
            path = source_database.parent / path
        inventory.append(
            {
                "instance_id": int(row["instance_id"]),
                "instance_key": str(row["instance_key"]),
                "source_path": str(path.resolve()),
                "size_bytes": int(row["size_bytes"]),
                "sha256": str(row["sha256"]).lower(),
            }
        )
    return inventory


def _verify_source_audio(
    inventory: Sequence[dict[str, Any]], source_database: Path
) -> None:
    for entry in inventory:
        source_path = Path(str(entry["source_path"]))
        if not source_path.is_absolute():
            source_path = source_database.parent / source_path
        source_path = source_path.resolve(strict=True)
        if source_path.stat().st_size != int(entry["size_bytes"]):
            raise RuntimeError(f"V2 audio changed during release backup: {source_path}")
        if _file_sha256(source_path) != str(entry["sha256"]):
            raise RuntimeError(f"V2 audio changed during release backup: {source_path}")


def _dataset_digest(
    source_fingerprint: str,
    schema_version: int,
    inventory: Sequence[dict[str, Any]],
) -> str:
    normalized = {
        "source_database_sha256": source_fingerprint,
        "source_schema_version": schema_version,
        "audio": [
            {
                "instance_id": int(entry["instance_id"]),
                "instance_key": str(entry["instance_key"]),
                "size_bytes": int(entry["size_bytes"]),
                "sha256": str(entry["sha256"]),
            }
            for entry in inventory
        ],
    }
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _verify_database(path: Path) -> None:
    connection = _open_read_only(path)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()
    if result != "ok":
        raise ValueError(f"release backup database integrity check failed: {result}")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _database_fingerprint(path: Path) -> str:
    main_sha256 = _file_sha256(path)
    wal_path = Path(f"{path}-wal")
    if not wal_path.is_file() or wal_path.stat().st_size == 0:
        return main_sha256
    digest = hashlib.sha256()
    digest.update(b"sqlite-main\0")
    digest.update(main_sha256.encode("ascii"))
    digest.update(b"\0sqlite-wal\0")
    digest.update(_file_sha256(wal_path).encode("ascii"))
    return digest.hexdigest()


def _require_read_only(path: Path) -> None:
    if path.stat().st_mode & stat.S_IWUSR:
        raise ValueError(f"release backup file is writable: {path}")


def _safe_filename(value: str) -> str:
    safe = "".join(
        character if character.isalnum() or character in ".-_" else "_"
        for character in value
    )
    return safe or "audio.bin"


__all__ = [
    "ReleaseBackup",
    "create_release_backups",
    "verify_release_backup",
]
