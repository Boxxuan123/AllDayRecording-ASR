from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AutomaticWorkflowStateStore:
    """Cross-process state and retry requests for receiver-owned workflows."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.requests_root = self.root / "retry-requests"

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.requests_root.mkdir(parents=True, exist_ok=True)

    def status(self, session_id: str) -> dict[str, Any] | None:
        return self._read(self._status_path(session_id), session_id=session_id)

    def statuses(self) -> tuple[dict[str, Any], ...]:
        if not self.root.is_dir():
            return ()
        values: list[dict[str, Any]] = []
        for path in self.root.glob("*.json"):
            value = self._read(path)
            if value is not None and isinstance(value.get("session_id"), str):
                values.append(value)
        values.sort(
            key=lambda value: (
                str(value.get("updated_at") or ""),
                str(value.get("session_id") or ""),
            ),
            reverse=True,
        )
        return tuple(values)

    def save(self, session_id: str, value: Mapping[str, Any]) -> None:
        if str(value.get("session_id") or session_id) != session_id:
            raise ValueError("automatic workflow state has a mismatched session id")
        self.initialize()
        self._write(self._status_path(session_id), value)

    def request_retry(self, session_id: str) -> dict[str, Any]:
        current = self.status(session_id)
        if current is None:
            raise KeyError(f"automatic workflow does not exist: {session_id}")
        if current.get("status") != "needs_attention":
            raise ValueError("only an automatic workflow needing attention can be retried")
        self.initialize()
        requested_at = _utc_now()
        request = {
            "version": 1,
            "session_id": session_id,
            "requested_at": requested_at,
        }
        self._write(self._request_path(session_id), request)
        current.update(
            {
                "status": "retry_requested",
                "detail": "已从审核收件箱请求重试，等待接收服务领取",
                "retry_requested_at": requested_at,
                "updated_at": requested_at,
            }
        )
        self.save(session_id, current)
        return {
            "session_id": session_id,
            "status": "retry_requested",
            "requested_at": requested_at,
        }

    def consume_retry_requests(self) -> tuple[dict[str, Any], ...]:
        if not self.requests_root.is_dir():
            return ()
        requests: list[dict[str, Any]] = []
        for path in sorted(self.requests_root.glob("*.json")):
            value = self._read(path)
            if value is None:
                continue
            session_id = value.get("session_id")
            if not isinstance(session_id, str) or self.status(session_id) is None:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            requests.append(value)
        return tuple(requests)

    def _status_path(self, session_id: str) -> Path:
        return self.root / f"{_key(session_id)}.json"

    def _request_path(self, session_id: str) -> Path:
        return self.requests_root / f"{_key(session_id)}.json"

    @staticmethod
    def _read(
        path: Path, *, session_id: str | None = None
    ) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict):
            return None
        if session_id is not None and value.get("session_id") != session_id:
            return None
        return value

    @staticmethod
    def _write(path: Path, value: Mapping[str, Any]) -> None:
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str),
            encoding="utf-8",
        )
        os.replace(temporary, path)


def _key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = ["AutomaticWorkflowStateStore"]
