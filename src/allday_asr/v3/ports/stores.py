from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol


@dataclass(frozen=True)
class StoredContent:
    storage_key: str
    sha256: str
    size_bytes: int
    media_id: str


class ContentStore(Protocol):
    def put_file(
        self, source: Path, *, expected_sha256: str | None = None
    ) -> StoredContent: ...

    def put_bytes(self, payload: bytes) -> StoredContent: ...

    def open(self, storage_key: str) -> BinaryIO: ...


__all__ = ["ContentStore", "StoredContent"]
