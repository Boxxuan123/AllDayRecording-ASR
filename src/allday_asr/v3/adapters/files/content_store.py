from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import BinaryIO

from allday_asr.v3.domain.ids import stable_ulid
from allday_asr.v3.ports.stores import StoredContent


class ContentAddressedStore:
    """Immutable SHA-256 addressed file store with atomic publication."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def put_file(
        self, source: Path, *, expected_sha256: str | None = None
    ) -> StoredContent:
        resolved = source.resolve(strict=True)
        source_digest, source_size = _digest_file(resolved)
        if expected_sha256 is not None and source_digest != expected_sha256.lower():
            raise ValueError(
                f"content digest mismatch: expected {expected_sha256}, got {source_digest}"
            )
        self.initialize()
        storage_key = _storage_key(source_digest)
        destination = self._resolve(storage_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            self._copy_atomic(resolved, destination, source_digest, source_size)
        else:
            existing_digest, existing_size = _digest_file(destination)
            if existing_digest != source_digest or existing_size != source_size:
                raise RuntimeError("content-addressed store entry is corrupted")
        return _stored(storage_key, source_digest, source_size)

    def put_bytes(self, payload: bytes) -> StoredContent:
        digest = hashlib.sha256(payload).hexdigest()
        self.initialize()
        storage_key = _storage_key(digest)
        destination = self._resolve(storage_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{digest}.", suffix=".part", dir=destination.parent
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        else:
            existing_digest, existing_size = _digest_file(destination)
            if existing_digest != digest or existing_size != len(payload):
                raise RuntimeError("content-addressed store entry is corrupted")
        return _stored(storage_key, digest, len(payload))

    def open(self, storage_key: str) -> BinaryIO:
        return self._resolve(storage_key).open("rb")

    def path_for(self, storage_key: str) -> Path:
        return self._resolve(storage_key)

    def _resolve(self, storage_key: str) -> Path:
        candidate = (self.root / storage_key).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError("storage key escapes the content store")
        return candidate

    @staticmethod
    def _copy_atomic(
        source: Path, destination: Path, expected_digest: str, expected_size: int
    ) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{expected_digest}.", suffix=".part", dir=destination.parent
        )
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        size = 0
        try:
            with source.open("rb") as input_file, os.fdopen(descriptor, "wb") as output:
                while chunk := input_file.read(1024 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                output.flush()
                os.fsync(output.fileno())
            if digest.hexdigest() != expected_digest or size != expected_size:
                raise RuntimeError("source changed while it was copied")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


def _storage_key(sha256: str) -> str:
    return f"{sha256[:2]}/{sha256[2:4]}/{sha256}"


def _stored(storage_key: str, sha256: str, size_bytes: int) -> StoredContent:
    return StoredContent(
        storage_key=storage_key,
        sha256=sha256,
        size_bytes=size_bytes,
        media_id=stable_ulid("media", sha256),
    )


def _digest_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


__all__ = ["ContentAddressedStore"]
