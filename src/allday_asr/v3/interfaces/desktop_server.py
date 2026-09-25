from __future__ import annotations

import json
import mimetypes
import secrets
import time
import webbrowser
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote

from allday_asr.v3.application import (
    InsightGenerationFailed,
    InsightGenerationUnavailable,
    ReminderGenerationFailed,
    ReminderGenerationUnavailable,
    UtteranceRevisionConflict,
)
from allday_asr.v3.bootstrap import V3Core, V3CorePaths, compose_v3_core
from allday_asr.v3.adapters.audio.tools import AudioClip, assemble_audio_clips
from allday_asr.v3.adapters.transfer.automation_state import (
    AutomaticWorkflowStateStore,
)
from allday_asr.v3.domain import new_ulid

from .desktop_http_contract import (
    ASSET_ROOT,
    _integer,
    _is_frontend_path,
    _location_without_token,
    _parse_range,
)
from .desktop_routes_get import DesktopGetRoutesMixin
from .desktop_routes_post import DesktopPostRoutesMixin



class V3DesktopApplication:
    def __init__(self, core: V3Core, *, token: str | None = None) -> None:
        self.core = core
        self.core.people.sample_worker.start()
        self.automatic_workflows = AutomaticWorkflowStateStore(
            core.paths.state_dir / "automation"
        )
        self.automatic_workflows.initialize()
        self.token = token or secrets.token_urlsafe(32)
        self.host = "127.0.0.1"
        self.port = 0

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def close(self) -> None:
        self.core.close()

    def list_reviews(self, limit: int) -> tuple[dict[str, Any], ...]:
        # Operational failures stay in the processing/data surfaces. The inbox
        # is reserved for decisions that change reviewed domain state.
        return self.core.desktop.list_reviews(limit)


class V3DesktopRequestHandler(
    DesktopGetRoutesMixin, DesktopPostRoutesMixin, BaseHTTPRequestHandler
):
    asset_root = ASSET_ROOT
    server: "V3DesktopHTTPServer"
    max_json_body = 1024 * 1024
    session_cookie_name = "allday_v3_session"

    @property
    def application(self) -> V3DesktopApplication:
        return self.server.application

    def do_GET(self) -> None:
        self._handle(self._dispatch_get)

    def do_POST(self) -> None:
        self._handle(self._dispatch_post)

    def _handle(self, callback) -> None:
        request_id = new_ulid()
        try:
            if not self._trusted_host():
                self._send_error(
                    HTTPStatus.MISDIRECTED_REQUEST,
                    "invalid_desktop_host",
                    "V3 工作台只接受本机地址。",
                    request_id=request_id,
                )
                return
            callback()
        except KeyError as exc:
            self._send_error(
                HTTPStatus.NOT_FOUND, "not_found", str(exc), request_id=request_id
            )
        except UtteranceRevisionConflict as exc:
            self._send_error(
                HTTPStatus.CONFLICT,
                "revision_conflict",
                str(exc),
                request_id=request_id,
            )
        except ReminderGenerationUnavailable as exc:
            self._send_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "codex_unavailable",
                str(exc),
                request_id=request_id,
            )
        except ReminderGenerationFailed as exc:
            self._send_error(
                HTTPStatus.BAD_GATEWAY,
                "codex_generation_failed",
                str(exc),
                request_id=request_id,
            )
        except InsightGenerationUnavailable as exc:
            self._send_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "codex_unavailable",
                str(exc),
                request_id=request_id,
            )
        except InsightGenerationFailed as exc:
            self._send_error(
                HTTPStatus.BAD_GATEWAY,
                "codex_generation_failed",
                str(exc),
                request_id=request_id,
            )
        except ValueError as exc:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                str(exc),
                request_id=request_id,
            )
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except Exception:
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                "V3 Core 无法完成请求。",
                request_id=request_id,
            )



    def _consume_token(self, parsed) -> bool:
        token = parse_qs(parsed.query).get("token", [None])[0]
        if token is None:
            return False
        if not secrets.compare_digest(token, self.application.token):
            self._send_error(
                HTTPStatus.FORBIDDEN, "invalid_desktop_link", "安全链接已失效。"
            )
            return True
        self._establish_session(_location_without_token(parsed))
        return True

    def _establish_session(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Set-Cookie", self._session_cookie())
        self._security_headers()
        self.end_headers()

    def _session_cookie(self) -> str:
        return (
            f"{self.session_cookie_name}={self.application.token}; "
            "Path=/; HttpOnly; SameSite=Strict"
        )

    def _recoverable_navigation(self, path: str) -> bool:
        if not _is_frontend_path(path):
            return False
        return (
            self.headers.get("Sec-Fetch-Mode") == "navigate"
            and self.headers.get("Sec-Fetch-Dest") == "document"
            and self.headers.get("Sec-Fetch-Site") in {"none", "same-origin"}
        )

    def _recoverable_fetch(self) -> bool:
        origin = self.headers.get("Origin")
        return (
            self.headers.get("X-AllDay-Desktop-Recovery") == "1"
            and self.headers.get("Sec-Fetch-Site") in {None, "same-origin"}
            and origin
            in {
                self.application.base_url,
                f"http://localhost:{self.application.port}",
            }
        )

    def _trusted_host(self) -> bool:
        authority = self.headers.get("Host", "").lower()
        return authority in {
            f"127.0.0.1:{self.application.port}",
            f"localhost:{self.application.port}",
        }

    def _authenticated(self) -> bool:
        header = self.headers.get("X-AllDay-Desktop-Session")
        if header and secrets.compare_digest(header, self.application.token):
            return True
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        session = cookie.get(self.session_cookie_name)
        return bool(
            session
            and secrets.compare_digest(session.value, self.application.token)
        )

    def _authorized_mutation(self) -> bool:
        if not self._authenticated():
            self._send_error(
                HTTPStatus.FORBIDDEN,
                "desktop_session_required",
                "V3 Desktop Session 无效。",
            )
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {
            self.application.base_url,
            f"http://localhost:{self.application.port}",
        }:
            self._send_error(
                HTTPStatus.FORBIDDEN, "cross_origin_denied", "拒绝跨站写请求。"
            )
            return False
        return True

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length is invalid") from exc
        if length < 0 or length > self.max_json_body:
            raise ValueError("request body size is invalid")
        if length == 0:
            return {}
        try:
            value = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ValueError("request body is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def _send_frontend(self, path: str) -> None:
        if _is_frontend_path(path):
            self._send_asset("index.html", "text/html; charset=utf-8")
            return
        if path.startswith("/assets/") or path in {"/favicon.svg", "/icons.svg"}:
            filename = unquote(path.lstrip("/"))
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            self._send_asset(filename, content_type)
            return
        self._send_error(HTTPStatus.NOT_FOUND, "not_found", "页面不存在。")

    def _send_asset(self, filename: str, content_type: str) -> None:
        root = self.asset_root.resolve()
        path = (root / filename).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise KeyError(f"frontend asset does not exist: {filename}")
        self._send_bytes(HTTPStatus.OK, path.read_bytes(), content_type)

    def _send_media(self, media_id: str) -> None:
        descriptor = self.application.core.desktop.media(media_id)
        size = int(descriptor["size_bytes"])
        start, end = _parse_range(self.headers.get("Range"), size)
        with self.application.core.audio_store.open(str(descriptor["storage_key"])) as source:
            source.seek(start)
            payload = source.read(end - start + 1)
        status = HTTPStatus.PARTIAL_CONTENT if start != 0 or end != size - 1 else HTTPStatus.OK
        content_type = {
            "wav": "audio/wav",
            "flac": "audio/flac",
            "m4a": "audio/mp4",
        }.get(str(descriptor["format"]), "application/octet-stream")
        headers = {"Accept-Ranges": "bytes"}
        if status is HTTPStatus.PARTIAL_CONTENT:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        self._send_bytes(status, payload, content_type, headers=headers)

    def _send_session_audio(
        self, session_id: str, *, start_ms: int, end_ms: int
    ) -> None:
        descriptors = self.application.core.desktop.session_audio_clips(
            session_id, start_ms, end_ms
        )
        clips = tuple(
            AudioClip(
                source=self.application.core.audio_store.path_for(
                    str(descriptor["storage_key"])
                ),
                start_ms=int(descriptor["start_ms"]),
                end_ms=int(descriptor["end_ms"]),
            )
            for descriptor in descriptors
        )
        output = (
            self.application.core.paths.state_dir
            / f".session-audio-{new_ulid()}.wav"
        )
        try:
            assemble_audio_clips(clips, output)
            payload = output.read_bytes()
        finally:
            output.unlink(missing_ok=True)
        self._send_bytes(
            HTTPStatus.OK,
            payload,
            "audio/wav",
            headers={"Accept-Ranges": "none"},
        )

    def _send_processing_events(self, query: dict[str, list[str]]) -> None:
        after = self.headers.get("Last-Event-ID") or query.get("after", ["0"])[0]
        sequence = _integer(after, "event sequence")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self._security_headers()
        self.end_headers()
        self.wfile.write(b"retry: 1500\n\nevent: ready\ndata: {}\n\n")
        self.wfile.flush()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            events = self.application.core.desktop.processing_events(sequence)
            if events:
                for event in events:
                    sequence = int(event["sequence"])
                    payload = json.dumps(
                        event["payload"], ensure_ascii=False, separators=(",", ":")
                    )
                    data = (
                        f"id: {sequence}\nevent: processing\ndata: {payload}\n\n"
                    ).encode("utf-8")
                    self.wfile.write(data)
                self.wfile.flush()
            else:
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
            time.sleep(0.5)

    def _send_json(self, status: HTTPStatus, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send_bytes(status, data, "application/json; charset=utf-8")

    def _send_error(
        self,
        status: HTTPStatus,
        code: str,
        message: str,
        *,
        request_id: str | None = None,
    ) -> None:
        self._send_json(
            status,
            {
                "code": code,
                "message": message,
                "details": {},
                "request_id": request_id or new_ulid(),
            },
        )

    def _send_bytes(
        self,
        status: HTTPStatus,
        data: bytes,
        content_type: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
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
            "connect-src 'self'; media-src 'self' blob:; img-src 'self'; "
            "frame-ancestors 'none'",
        )

    def log_message(self, format: str, *args) -> None:
        print(f"[v3-desktop] {self.address_string()} {format % args}")


class V3DesktopHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, application: V3DesktopApplication):
        self.application = application
        super().__init__(server_address, V3DesktopRequestHandler)

    def server_close(self) -> None:
        try:
            self.application.close()
        finally:
            super().server_close()


def create_v3_desktop_server(
    *,
    paths: V3CorePaths,
    host: str = "127.0.0.1",
    port: int = 8766,
    token: str | None = None,
) -> V3DesktopHTTPServer:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("V3 Desktop API can only bind to loopback")
    core = compose_v3_core(paths)
    core.initialize()
    application = V3DesktopApplication(core, token=token)
    server = V3DesktopHTTPServer((host, port), application)
    bound_host, bound_port = server.server_address[:2]
    application.host = "127.0.0.1" if bound_host in {"0.0.0.0", "::"} else host
    application.port = int(bound_port)
    return server


def serve_v3_desktop(
    *,
    paths: V3CorePaths,
    host: str = "127.0.0.1",
    port: int = 8766,
    open_browser: bool = True,
) -> None:
    server = create_v3_desktop_server(paths=paths, host=host, port=port)
    url = f"{server.application.base_url}/"
    print(f"AllDayRecording V3 工作台：{url}")
    print("这个短地址可直接重新打开或刷新；浏览器会自动恢复本机会话。")
    print("仅监听本机；按 Ctrl+C 停止。")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()




__all__ = [
    "V3DesktopApplication",
    "V3DesktopHTTPServer",
    "V3DesktopRequestHandler",
    "create_v3_desktop_server",
    "serve_v3_desktop",
]
