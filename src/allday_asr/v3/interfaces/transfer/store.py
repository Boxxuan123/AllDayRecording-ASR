from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


DEFAULT_MAX_FILE_BYTES = 16 * 1024 * 1024 * 1024
DEFAULT_MAX_CHUNK_BYTES = 4 * 1024 * 1024
SUPPORTED_SUFFIXES = {
    "recording": frozenset({".aac", ".flac", ".m4a", ".ogg", ".opus", ".wav"}),
    "manifest": frozenset({".json"}),
}
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_UPLOAD_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_INVALID_WINDOWS_CHARS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
KnownCompletedUpload = Callable[[str, int, str, str], bool]


class UploadStoreError(ValueError):
    """Base class for rejected upload operations."""


class UploadNotFoundError(UploadStoreError):
    pass


class UploadConflictError(UploadStoreError):
    pass


class UploadOffsetError(UploadConflictError):
    def __init__(self, expected_offset: int, actual_offset: int):
        self.expected_offset = expected_offset
        self.actual_offset = actual_offset
        super().__init__(
            f"上传偏移不一致：服务端需要 {expected_offset}，客户端发送 {actual_offset}"
        )


class UploadDigestError(UploadStoreError):
    def __init__(self, expected_digest: str, actual_digest: str):
        self.expected_digest = expected_digest
        self.actual_digest = actual_digest
        super().__init__("文件 SHA-256 校验失败，临时内容已清除，可从偏移 0 重传")


@dataclass(frozen=True)
class UploadRecord:
    upload_id: str
    relative_path: str
    size: int
    sha256: str
    kind: str
    status: str
    offset: int
    created_at: str
    updated_at: str

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


class UploadStore:
    """Durable upload state and immutable final files below one inbox root."""

    def __init__(
        self,
        root: Path,
        *,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_chunk_bytes: int = DEFAULT_MAX_CHUNK_BYTES,
        known_completed: KnownCompletedUpload | None = None,
    ) -> None:
        if max_file_bytes <= 0 or max_chunk_bytes <= 0:
            raise ValueError("文件和分片大小上限必须大于 0")
        self.root = root.expanduser().resolve()
        self.state_root = self.root / ".uploads"
        self.max_file_bytes = max_file_bytes
        self.max_chunk_bytes = max_chunk_bytes
        self._known_completed = known_completed
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def create_upload(
        self,
        *,
        relative_path: str,
        size: int,
        sha256: str,
        kind: str,
    ) -> tuple[UploadRecord, bool]:
        normalized_path = _validate_relative_path(relative_path, kind=kind)
        normalized_digest = _validate_digest(sha256)
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise UploadStoreError("size 必须是大于 0 的整数")
        if size > self.max_file_bytes:
            raise UploadStoreError(
                f"文件超过接收上限：{size} > {self.max_file_bytes} bytes"
            )
        upload_id = _upload_id(
            normalized_path,
            size=size,
            sha256=normalized_digest,
            kind=kind,
        )
        with self._lock:
            existing = self._load_optional(upload_id)
            if existing is not None:
                return self._refresh_offset(existing), False

            destination = self._destination(normalized_path)
            if destination.exists():
                if not destination.is_file():
                    raise UploadConflictError(f"接收路径已被目录占用：{normalized_path}")
                actual_size = destination.stat().st_size
                actual_digest = _sha256_file(destination) if actual_size == size else ""
                if actual_size != size or actual_digest != normalized_digest:
                    raise UploadConflictError(
                        f"接收路径已有不同内容，服务不会覆盖：{normalized_path}"
                    )
                now = _utc_now()
                record = UploadRecord(
                    upload_id=upload_id,
                    relative_path=normalized_path,
                    size=size,
                    sha256=normalized_digest,
                    kind=kind,
                    status="completed",
                    offset=size,
                    created_at=now,
                    updated_at=now,
                )
                self._write_record(record)
                return record, False

            if self._known_completed is not None and self._known_completed(
                normalized_path,
                size,
                normalized_digest,
                kind,
            ):
                now = _utc_now()
                return (
                    UploadRecord(
                        upload_id=upload_id,
                        relative_path=normalized_path,
                        size=size,
                        sha256=normalized_digest,
                        kind=kind,
                        status="completed",
                        offset=size,
                        created_at=now,
                        updated_at=now,
                    ),
                    False,
                )

            now = _utc_now()
            record = UploadRecord(
                upload_id=upload_id,
                relative_path=normalized_path,
                size=size,
                sha256=normalized_digest,
                kind=kind,
                status="uploading",
                offset=0,
                created_at=now,
                updated_at=now,
            )
            self._write_record(record)
            return record, True

    def has_completed_file(self, record: UploadRecord) -> bool:
        """Return whether this inbox physically contains the completed payload."""

        if record.status != "completed":
            return False
        with self._lock:
            destination = self._destination(record.relative_path)
            return (
                destination.is_file()
                and destination.stat().st_size == record.size
                and _sha256_file(destination) == record.sha256
            )

    def get_upload(self, upload_id: str) -> UploadRecord:
        with self._lock:
            return self._refresh_offset(self._load(upload_id))

    def list_uploads(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        relative_paths: set[str] | None = None,
    ) -> list[UploadRecord]:
        if kind is not None and kind not in SUPPORTED_SUFFIXES:
            raise UploadStoreError("kind 只能是 recording 或 manifest")
        if status is not None and status not in {"uploading", "completed"}:
            raise UploadStoreError("status 只能是 uploading 或 completed")
        with self._lock:
            records: list[UploadRecord] = []
            for path in sorted(self.state_root.glob("*.json")):
                if not _UPLOAD_ID_PATTERN.fullmatch(path.stem):
                    continue
                record = self._load(path.stem)
                if kind is not None and record.kind != kind:
                    continue
                if relative_paths is not None and record.relative_path not in relative_paths:
                    continue
                record = self._refresh_offset(record)
                if status is not None and record.status != status:
                    continue
                records.append(record)
            return records

    def completed_path(self, record: UploadRecord) -> Path:
        """Return a verified inbox path for one immutable completed upload."""
        with self._lock:
            refreshed = self._refresh_offset(self._load(record.upload_id))
            if refreshed != record or refreshed.status != "completed":
                raise UploadConflictError("上传记录不是当前已完成版本")
            return self._destination(refreshed.relative_path)

    def append_chunk(
        self,
        upload_id: str,
        *,
        offset: int,
        data: bytes,
    ) -> UploadRecord:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise UploadStoreError("Upload-Offset 必须是非负整数")
        if not data:
            raise UploadStoreError("上传分片不能为空")
        if len(data) > self.max_chunk_bytes:
            raise UploadStoreError(
                f"上传分片超过上限：{len(data)} > {self.max_chunk_bytes} bytes"
            )

        with self._lock:
            record = self._refresh_offset(self._load(upload_id))
            if record.status == "completed":
                if offset != record.size:
                    raise UploadOffsetError(record.size, offset)
                raise UploadConflictError("文件已经完整接收，不能追加更多字节")
            if offset != record.offset:
                raise UploadOffsetError(record.offset, offset)
            if record.offset + len(data) > record.size:
                raise UploadStoreError("上传分片超过声明的文件总大小")

            part_path = self._part_path(record.upload_id)
            with part_path.open("ab") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            updated = replace(
                record,
                offset=record.offset + len(data),
                updated_at=_utc_now(),
            )
            if updated.offset < updated.size:
                self._write_record(updated)
                return updated
            return self._finalize(updated)

    def _finalize(self, record: UploadRecord) -> UploadRecord:
        part_path = self._part_path(record.upload_id)
        actual_digest = _sha256_file(part_path)
        if actual_digest != record.sha256:
            part_path.unlink(missing_ok=True)
            reset = replace(record, offset=0, updated_at=_utc_now())
            self._write_record(reset)
            raise UploadDigestError(record.sha256, actual_digest)

        destination = self._destination(record.relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise UploadConflictError(
                f"接收路径在上传期间被其他内容占用：{record.relative_path}"
            )
        try:
            os.link(part_path, destination)
        except FileExistsError as exc:
            raise UploadConflictError(
                f"接收路径在上传期间被其他内容占用：{record.relative_path}"
            ) from exc
        part_path.unlink()
        completed = replace(
            record,
            status="completed",
            offset=record.size,
            updated_at=_utc_now(),
        )
        self._write_record(completed)
        return completed

    def _refresh_offset(self, record: UploadRecord) -> UploadRecord:
        if record.status == "completed":
            destination = self._destination(record.relative_path)
            if not destination.is_file() or destination.stat().st_size != record.size:
                raise UploadConflictError(
                    f"已完成文件缺失或大小改变：{record.relative_path}"
                )
            if _sha256_file(destination) != record.sha256:
                raise UploadConflictError(
                    f"已完成文件的 SHA-256 已改变：{record.relative_path}"
                )
            return record
        destination = self._destination(record.relative_path)
        if destination.exists():
            if not destination.is_file() or destination.stat().st_size != record.size:
                raise UploadConflictError(
                    f"上传期间接收路径出现不同内容：{record.relative_path}"
                )
            if _sha256_file(destination) != record.sha256:
                raise UploadConflictError(
                    f"上传期间接收路径出现不同内容：{record.relative_path}"
                )
            completed = replace(
                record,
                status="completed",
                offset=record.size,
                updated_at=_utc_now(),
            )
            self._part_path(record.upload_id).unlink(missing_ok=True)
            self._write_record(completed)
            return completed
        part_path = self._part_path(record.upload_id)
        actual_offset = part_path.stat().st_size if part_path.is_file() else 0
        if actual_offset > record.size:
            raise UploadConflictError("临时上传文件大于声明大小，请人工检查 inbox")
        if actual_offset == record.offset:
            return record
        refreshed = replace(record, offset=actual_offset, updated_at=_utc_now())
        self._write_record(refreshed)
        return refreshed

    def _destination(self, relative_path: str) -> Path:
        destination = (self.root / Path(*PurePosixPath(relative_path).parts)).resolve()
        try:
            destination.relative_to(self.root)
        except ValueError as exc:
            raise UploadStoreError("接收路径越过 inbox 根目录") from exc
        return destination

    def _metadata_path(self, upload_id: str) -> Path:
        _validate_upload_id(upload_id)
        return self.state_root / f"{upload_id}.json"

    def _part_path(self, upload_id: str) -> Path:
        _validate_upload_id(upload_id)
        return self.state_root / f"{upload_id}.part"

    def _load_optional(self, upload_id: str) -> UploadRecord | None:
        path = self._metadata_path(upload_id)
        if not path.is_file():
            return None
        return self._load(upload_id)

    def _load(self, upload_id: str) -> UploadRecord:
        path = self._metadata_path(upload_id)
        if not path.is_file():
            raise UploadNotFoundError(f"上传任务不存在：{upload_id}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            record = UploadRecord(**payload)
        except (OSError, TypeError, json.JSONDecodeError) as exc:
            raise UploadConflictError(f"上传状态损坏：{upload_id}") from exc
        normalized_path = _validate_relative_path(record.relative_path, kind=record.kind)
        expected_id = _upload_id(
            normalized_path,
            size=record.size,
            sha256=_validate_digest(record.sha256),
            kind=record.kind,
        )
        if expected_id != upload_id or record.upload_id != upload_id:
            raise UploadConflictError(f"上传状态身份不匹配：{upload_id}")
        if record.status not in {"uploading", "completed"}:
            raise UploadConflictError(f"上传状态值无效：{upload_id}")
        return record

    def _write_record(self, record: UploadRecord) -> None:
        path = self._metadata_path(record.upload_id)
        temporary = self.state_root / f".{record.upload_id}.json.tmp"
        payload = json.dumps(
            asdict(record),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)


def _validate_relative_path(value: str, *, kind: str) -> str:
    if not isinstance(kind, str) or kind not in SUPPORTED_SUFFIXES:
        raise UploadStoreError("kind 只能是 recording 或 manifest")
    if not isinstance(value, str) or not value or len(value) > 512:
        raise UploadStoreError("relative_path 不能为空且不能超过 512 个字符")
    if "\\" in value or value.startswith("/") or value.endswith("/"):
        raise UploadStoreError("relative_path 必须使用 / 分隔的相对路径")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts:
        raise UploadStoreError("relative_path 必须是相对路径")
    if path.as_posix() != value:
        raise UploadStoreError("relative_path 必须是规范路径，不能包含重复 / 或 .")
    for component in path.parts:
        if component in {"", ".", ".."} or component.startswith("."):
            raise UploadStoreError("relative_path 不能包含隐藏目录、. 或 ..")
        if len(component) > 128 or component.endswith((" ", ".")):
            raise UploadStoreError("relative_path 含有过长或不可移植的路径段")
        if any(ord(character) < 32 for character in component):
            raise UploadStoreError("relative_path 不能包含控制字符")
        if any(character in _INVALID_WINDOWS_CHARS for character in component):
            raise UploadStoreError("relative_path 包含 Windows 不支持的字符")
        if component.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
            raise UploadStoreError("relative_path 包含 Windows 保留名称")
    if path.suffix.lower() not in SUPPORTED_SUFFIXES[kind]:
        allowed = ", ".join(sorted(SUPPORTED_SUFFIXES[kind]))
        raise UploadStoreError(f"{kind} 文件扩展名必须是：{allowed}")
    return path.as_posix()


def _validate_digest(value: str) -> str:
    if not isinstance(value, str):
        raise UploadStoreError("sha256 必须是 64 位十六进制字符串")
    digest = value.lower()
    if not _SHA256_PATTERN.fullmatch(digest):
        raise UploadStoreError("sha256 必须是 64 位十六进制字符串")
    return digest


def _validate_upload_id(value: str) -> None:
    if not isinstance(value, str) or not _UPLOAD_ID_PATTERN.fullmatch(value):
        raise UploadNotFoundError("上传任务 ID 无效")


def _upload_id(relative_path: str, *, size: int, sha256: str, kind: str) -> str:
    identity = json.dumps(
        {
            "kind": kind,
            "relative_path": relative_path,
            "sha256": sha256,
            "size": size,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()[:32]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
