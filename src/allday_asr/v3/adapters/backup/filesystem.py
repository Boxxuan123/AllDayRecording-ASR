from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from allday_asr.domain.hashing import canonical_json_sha256
from allday_asr.v3.adapters.files import ContentAddressedStore
from allday_asr.v3.adapters.sqlite import V3Database


_SAFE_SESSION_DIRECTORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class FilesystemBackupResult:
    session_id: str
    provider: str
    storage_kind: str
    digest: str
    file_count: int
    byte_count: int
    destination: Path


class FilesystemSessionBackupAdapter:
    """Copy a V3 session to a declared independent root and read it back."""

    def __init__(
        self,
        database: V3Database,
        audio_store: ContentAddressedStore,
        artifact_store: ContentAddressedStore,
    ) -> None:
        self.database = database
        self.audio_store = audio_store
        self.artifact_store = artifact_store

    def backup(
        self, session_id: str, backup_root: Path, *, storage_kind: str
    ) -> FilesystemBackupResult:
        if storage_kind not in {"independent_device", "network"}:
            raise ValueError("backup storage kind must be independent_device or network")
        if _SAFE_SESSION_DIRECTORY.fullmatch(session_id) is None:
            raise ValueError("session id is not safe for a backup directory")
        root = backup_root.expanduser().resolve()
        self._validate_independent_root(root)
        entries = self._entries(session_id)
        root.mkdir(parents=True, exist_ok=True)
        final = root / session_id
        # Keep the staging name short enough for Windows' legacy MAX_PATH handling.
        # The final directory still carries the stable session identifier.
        temporary = root / f".stage-{uuid4().hex}"
        temporary.mkdir()
        try:
            copied: list[dict[str, object]] = []
            for relative, source, expected_sha256 in entries:
                destination = temporary / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                digest, size = _digest(destination)
                if digest != expected_sha256:
                    raise RuntimeError("backup copy failed SHA-256 verification")
                copied.append(
                    {"path": relative.as_posix(), "sha256": digest, "size": size}
                )
            digest = canonical_json_sha256(copied)
            if final.exists():
                existing = self._verify_existing(final, copied)
                if existing != digest:
                    raise RuntimeError("existing backup destination has conflicting content")
            else:
                os.replace(temporary, final)
            restored = self._verify_existing(final, copied)
            if restored != digest:
                raise RuntimeError("backup restore drill changed session digest")
            return FilesystemBackupResult(
                session_id=session_id,
                provider=f"filesystem:{root.name}",
                storage_kind=storage_kind,
                digest=digest,
                file_count=len(copied),
                byte_count=sum(int(value["size"]) for value in copied),
                destination=final,
            )
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    def _entries(self, session_id: str) -> list[tuple[Path, Path, str]]:
        with self.database.read() as connection:
            manifest = connection.execute(
                """
                SELECT sha256, storage_ref FROM session_manifests
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT s.sequence, a.sha256, a.format, r.storage_key
                FROM capture_segments s
                JOIN audio_assets a ON a.asset_id = s.asset_id
                JOIN audio_replicas r ON r.replica_id = s.replica_id
                WHERE s.session_id = ? AND r.state = 'available'
                ORDER BY s.sequence, s.segment_id
                """,
                (session_id,),
            ).fetchall()
        if manifest is None or not rows:
            raise ValueError("session is incomplete and cannot be backed up")
        values = [
            (
                Path("manifest") / "session_summary.json",
                self.artifact_store.path_for(str(manifest["storage_ref"])),
                str(manifest["sha256"]),
            )
        ]
        values.extend(
            (
                Path("audio")
                / f"{int(row['sequence']):06d}-{row['sha256']}.{row['format']}",
                self.audio_store.path_for(str(row["storage_key"])),
                str(row["sha256"]),
            )
            for row in rows
        )
        return values

    def _validate_independent_root(self, root: Path) -> None:
        for source in (self.audio_store.root, self.artifact_store.root):
            if root == source or root.is_relative_to(source) or source.is_relative_to(root):
                raise ValueError("backup root must not overlap a V3 content store")

    @staticmethod
    def _verify_existing(root: Path, expected: list[dict[str, object]]) -> str:
        restored: list[dict[str, object]] = []
        for value in expected:
            path = root / str(value["path"])
            digest, size = _digest(path)
            if digest != value["sha256"] or size != value["size"]:
                raise RuntimeError("backup restore drill found corrupt content")
            restored.append(
                {"path": value["path"], "sha256": digest, "size": size}
            )
        return canonical_json_sha256(restored)


def _digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


__all__ = ["FilesystemBackupResult", "FilesystemSessionBackupAdapter"]
