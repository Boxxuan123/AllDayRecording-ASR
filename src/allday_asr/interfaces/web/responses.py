from __future__ import annotations

import json
from http import HTTPStatus
from pathlib import Path
from typing import Any


class LocalResponseMixin:
    """JSON, static-file and security-header responses for loopback servers."""

    asset_root: Path
    max_json_body = 1024 * 1024

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if length <= 0 or length > self.max_json_body:
            raise ValueError("请求正文大小无效")
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ValueError("请求正文不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("请求正文必须是 JSON 对象")
        return payload

    def _send_asset(self, filename: str, content_type: str) -> None:
        root = self.asset_root.resolve()
        path = (root / filename).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise FileNotFoundError(f"网页资源不存在：{filename}") from exc
        if not path.is_file():
            raise FileNotFoundError(f"网页资源不存在：{filename}")
        self._send_bytes(HTTPStatus.OK, path.read_bytes(), content_type)

    def _send_file(self, path: Path, content_type: str) -> None:
        self._send_bytes(
            HTTPStatus.OK,
            path.read_bytes(),
            content_type,
            cache_control="private, max-age=3600",
        )

    def _send_json(self, status: HTTPStatus, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send_bytes(status, data, "application/json; charset=utf-8")

    def _send_bytes(
        self,
        status: HTTPStatus,
        data: bytes,
        content_type: str,
        *,
        cache_control: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache_control)
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; media-src 'self'; img-src 'self'; "
            "frame-ancestors 'none'",
        )
