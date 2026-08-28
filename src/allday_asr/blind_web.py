from __future__ import annotations

import json
import secrets
import threading
import uuid
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from allday_asr.services.benchmark import (
    BLIND_PROTOCOL_FORMAT,
    CONTINUOUS_TRUTH_FORMAT,
)

BLIND_ASSET_ROOT = Path(__file__).parent / "web_assets"
MAX_JSON_BODY = 1024 * 1024
FORBIDDEN_BLIND_FIELDS = {
    "asr_output",
    "hypothesis_text",
    "hypothesis_text_at_export",
    "legacy_segment_id",
    "model_output",
    "prediction_text",
    "segment_id",
}


class BlindAnnotationApplication:
    """Isolated editor that never opens the database or any model output."""

    def __init__(self, task_path: Path, *, token: str | None = None):
        self.task_path = task_path.resolve(strict=True)
        self.token = token or secrets.token_urlsafe(32)
        self.host = "127.0.0.1"
        self.port = 0
        self.lock = threading.Lock()
        self._validate_task_identity(self._read_rows())

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def task_payload(self) -> dict[str, Any]:
        with self.lock:
            rows = self._read_rows()
        metadata = rows[0]
        windows = [row for row in rows if row.get("type") == "blind_window"]
        utterances = self._utterance_payloads(rows)
        counts = {
            int(window["window_index"]): sum(
                utterance["window_index"] == int(window["window_index"])
                for utterance in utterances
            )
            for window in windows
        }
        return {
            "name": metadata["name"],
            "scope_start_ms": int(metadata["scope_start_ms"]),
            "scope_end_ms": int(metadata["scope_end_ms"]),
            "completeness": metadata.get("completeness", {}),
            "blind_attestation": metadata.get("blind_attestation", {}),
            "finalized": (
                metadata.get("blind_attestation", {}).get("model_outputs_unseen")
                is True
            ),
            "windows": [
                {
                    "window_index": int(window["window_index"]),
                    "session_start_ms": int(window["session_start_ms"]),
                    "session_end_ms": int(window["session_end_ms"]),
                    "review_status": str(window.get("review_status", "pending")),
                    "notes": str(window.get("notes", "")),
                    "audio_url": f"/audio/{int(window['window_index'])}",
                    "utterance_count": counts[int(window["window_index"])],
                }
                for window in windows
            ],
            "utterances": utterances,
        }

    def save_utterance(self, payload: dict[str, Any]) -> dict[str, Any]:
        window_index = _required_nonnegative_int(payload, "window_index")
        start_ms = _required_nonnegative_int(payload, "start_ms")
        end_ms = _required_nonnegative_int(payload, "end_ms")
        if end_ms <= start_ms:
            raise ValueError("结束时间必须晚于开始时间")
        text = str(payload.get("text") or "").strip()
        unintelligible = payload.get("unintelligible") is True
        if bool(text) == unintelligible:
            raise ValueError("必须填写听写文字，或选择“听不清”且不填写文字")
        if len(text) > 4_000:
            raise ValueError("单条听写文字不能超过 4000 字")
        supplied_id = str(payload.get("utterance_id") or "").strip()
        if supplied_id and not supplied_id.replace("-", "").isalnum():
            raise ValueError("utterance_id 无效")

        with self.lock:
            rows = self._read_rows()
            self._ensure_editable(rows)
            window = self._window(rows, window_index)
            duration_ms = int(window["session_end_ms"]) - int(
                window["session_start_ms"]
            )
            if start_ms >= duration_ms or end_ms > duration_ms:
                raise ValueError("标注时间超出当前五分钟窗口")
            utterance_id = supplied_id or uuid.uuid4().hex
            old_window_indexes = self._utterance_window_indexes(rows, utterance_id)
            rows = [
                row
                for row in rows
                if not (
                    row.get("type") == "annotation"
                    and _row_utterance_id(row) == utterance_id
                )
            ]
            absolute_start = int(window["session_start_ms"]) + start_ms
            absolute_end = int(window["session_start_ms"]) + end_ms
            metadata = {
                "reviewed": True,
                "utterance_id": utterance_id,
                "window_index": window_index,
                "created_by": "blind-web-v1",
            }
            rows.append(
                _annotation_row(
                    f"blind:speech:{utterance_id}",
                    "speech",
                    absolute_start,
                    absolute_end,
                    label="speech",
                    metadata=metadata,
                )
            )
            if unintelligible:
                rows.append(
                    _annotation_row(
                        f"blind:uncertain:{utterance_id}",
                        "uncertain",
                        absolute_start,
                        absolute_end,
                        label="unintelligible",
                        metadata=metadata,
                    )
                )
            else:
                rows.append(
                    _annotation_row(
                        f"blind:transcript:{utterance_id}",
                        "transcript",
                        absolute_start,
                        absolute_end,
                        text=text,
                        metadata=metadata,
                    )
                )
            self._invalidate(rows, {*old_window_indexes, window_index})
            self._write_rows(rows)
        return self._find_utterance(self.task_payload()["utterances"], utterance_id)

    def delete_utterance(self, utterance_id: str) -> None:
        with self.lock:
            rows = self._read_rows()
            self._ensure_editable(rows)
            window_indexes = self._utterance_window_indexes(rows, utterance_id)
            if not window_indexes:
                raise KeyError(f"标注 {utterance_id} 不存在")
            rows = [
                row
                for row in rows
                if not (
                    row.get("type") == "annotation"
                    and _row_utterance_id(row) == utterance_id
                )
            ]
            self._invalidate(rows, window_indexes)
            self._write_rows(rows)

    def set_window_status(self, window_index: int, status: str) -> dict[str, Any]:
        if status not in {"pending", "complete"}:
            raise ValueError("review_status 必须是 pending 或 complete")
        with self.lock:
            rows = self._read_rows()
            self._ensure_editable(rows)
            window = self._window(rows, window_index)
            window["review_status"] = status
            self._reset_finalization(rows)
            self._write_rows(rows)
        return self._window_payload(self.task_payload(), window_index)

    def finalize(self, annotator: str, *, confirm_unseen: bool) -> dict[str, Any]:
        annotator = annotator.strip()
        if not annotator:
            raise ValueError("请填写标注者姓名或代号")
        if not confirm_unseen:
            raise ValueError("必须确认没有查看这 30 分钟对应的模型输出")
        with self.lock:
            rows = self._read_rows()
            windows = [row for row in rows if row.get("type") == "blind_window"]
            pending = [
                int(window["window_index"])
                for window in windows
                if window.get("review_status") != "complete"
            ]
            if pending:
                raise ValueError(f"仍有未完整检查的窗口：{pending}")
            self._validate_utterance_pairs(rows)
            metadata = rows[0]
            metadata["completeness"]["vad"] = "exhaustive"
            metadata["completeness"]["transcript"] = "exhaustive"
            metadata["blind_attestation"] = {
                "model_outputs_unseen": True,
                "annotator": annotator,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            self._write_rows(rows)
        return self.task_payload()

    def audio_path(self, window_index: int) -> Path:
        with self.lock:
            rows = self._read_rows()
            window = self._window(rows, window_index)
        filename = str(window.get("audio_file") or "")
        if Path(filename).name != filename:
            raise ValueError("盲标音频路径无效")
        path = (self.task_path.parent / filename).resolve(strict=True)
        if path.parent != self.task_path.parent:
            raise ValueError("盲标音频必须位于任务目录")
        return path

    def _read_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for line_number, line in enumerate(
            self.task_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"任务文件第 {line_number} 行不是有效 JSON") from exc
            if not isinstance(row, dict):
                raise ValueError(f"任务文件第 {line_number} 行必须是 JSON 对象")
            rows.append(row)
        return rows

    def _write_rows(self, rows: list[dict[str, Any]]) -> None:
        temporary = self.task_path.with_suffix(self.task_path.suffix + ".web.tmp")
        temporary.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.task_path)

    @staticmethod
    def _validate_task_identity(rows: list[dict[str, Any]]) -> None:
        if not rows or rows[0].get("type") != "metadata":
            raise ValueError("盲标任务第一行必须是 metadata")
        metadata = rows[0]
        provenance = metadata.get("provenance", {})
        if metadata.get("format") != CONTINUOUS_TRUTH_FORMAT or not isinstance(
            provenance, dict
        ):
            raise ValueError("不是受支持的连续真值文件")
        if provenance.get("protocol") != BLIND_PROTOCOL_FORMAT:
            raise ValueError("不是 V2-C.1 盲标任务")
        if provenance.get("model_outputs_used_for_selection") is not False:
            raise ValueError("任务范围不是独立盲选，拒绝打开")
        unsupported = [
            row.get("type")
            for row in rows
            if row.get("type") not in {"metadata", "blind_window", "annotation"}
        ]
        if unsupported:
            raise ValueError(f"盲标文件包含不支持的行类型：{unsupported}")

        def inspect(value: Any) -> None:
            if isinstance(value, dict):
                present = FORBIDDEN_BLIND_FIELDS & set(value)
                if present:
                    raise ValueError(
                        f"盲标文件包含被禁止的模型/V1 字段：{sorted(present)}"
                    )
                for nested in value.values():
                    inspect(nested)
            elif isinstance(value, list):
                for nested in value:
                    inspect(nested)

        inspect(rows)
        if not any(row.get("type") == "blind_window" for row in rows):
            raise ValueError("盲标任务没有音频窗口")

    @staticmethod
    def _ensure_editable(rows: list[dict[str, Any]]) -> None:
        if rows[0].get("blind_attestation", {}).get("model_outputs_unseen") is True:
            raise ValueError("任务已经最终确认；如需修改请先复制为新修订版")

    @staticmethod
    def _window(rows: list[dict[str, Any]], window_index: int) -> dict[str, Any]:
        for row in rows:
            if row.get("type") == "blind_window" and int(row["window_index"]) == window_index:
                return row
        raise KeyError(f"盲标窗口 {window_index} 不存在")

    @staticmethod
    def _invalidate(rows: list[dict[str, Any]], window_indexes: set[int]) -> None:
        for row in rows:
            if (
                row.get("type") == "blind_window"
                and int(row["window_index"]) in window_indexes
            ):
                row["review_status"] = "pending"
        BlindAnnotationApplication._reset_finalization(rows)

    @staticmethod
    def _reset_finalization(rows: list[dict[str, Any]]) -> None:
        metadata = rows[0]
        metadata["completeness"]["vad"] = "pending"
        metadata["completeness"]["transcript"] = "pending"
        metadata["blind_attestation"] = {
            "model_outputs_unseen": False,
            "annotator": "",
            "completed_at": None,
        }

    @staticmethod
    def _utterance_window_indexes(
        rows: list[dict[str, Any]], utterance_id: str
    ) -> set[int]:
        return {
            int(row.get("metadata", {}).get("window_index"))
            for row in rows
            if row.get("type") == "annotation"
            and _row_utterance_id(row) == utterance_id
            and row.get("metadata", {}).get("window_index") is not None
        }

    @staticmethod
    def _utterance_payloads(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        windows = {
            int(row["window_index"]): row
            for row in rows
            if row.get("type") == "blind_window"
        }
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            utterance_id = _row_utterance_id(row)
            if row.get("type") == "annotation" and utterance_id:
                grouped.setdefault(utterance_id, []).append(row)
        payloads: list[dict[str, Any]] = []
        for utterance_id, utterance_annotations in grouped.items():
            speech = next(
                (
                    row
                    for row in utterance_annotations
                    if row.get("kind") == "speech"
                ),
                None,
            )
            transcript = next(
                (
                    row
                    for row in utterance_annotations
                    if row.get("kind") == "transcript"
                ),
                None,
            )
            uncertain = next(
                (
                    row
                    for row in utterance_annotations
                    if row.get("kind") == "uncertain"
                    and row.get("label") == "unintelligible"
                ),
                None,
            )
            anchor = speech or transcript or uncertain
            if anchor is None:
                continue
            window_index = int(anchor.get("metadata", {}).get("window_index"))
            window = windows.get(window_index)
            if window is None:
                continue
            payloads.append(
                {
                    "utterance_id": utterance_id,
                    "window_index": window_index,
                    "start_ms": int(anchor["session_start_ms"])
                    - int(window["session_start_ms"]),
                    "end_ms": int(anchor["session_end_ms"])
                    - int(window["session_start_ms"]),
                    "text": str(transcript.get("text") or "") if transcript else "",
                    "unintelligible": uncertain is not None,
                }
            )
        return sorted(
            payloads,
            key=lambda item: (item["window_index"], item["start_ms"], item["end_ms"]),
        )

    @staticmethod
    def _validate_utterance_pairs(rows: list[dict[str, Any]]) -> None:
        utterances = BlindAnnotationApplication._utterance_payloads(rows)
        speech_ids = {
            _row_utterance_id(row)
            for row in rows
            if row.get("type") == "annotation" and row.get("kind") == "speech"
        }
        valid_ids = {
            utterance["utterance_id"]
            for utterance in utterances
            if bool(utterance["text"]) != bool(utterance["unintelligible"])
        }
        incomplete = sorted(item for item in speech_ids if item and item not in valid_ids)
        if incomplete:
            raise ValueError(f"以下 speech 缺少 transcript/听不清标记：{incomplete}")

    @staticmethod
    def _find_utterance(
        utterances: list[dict[str, Any]], utterance_id: str
    ) -> dict[str, Any]:
        for utterance in utterances:
            if utterance["utterance_id"] == utterance_id:
                return utterance
        raise KeyError(f"标注 {utterance_id} 不存在")

    @staticmethod
    def _window_payload(payload: dict[str, Any], window_index: int) -> dict[str, Any]:
        for window in payload["windows"]:
            if window["window_index"] == window_index:
                return window
        raise KeyError(f"盲标窗口 {window_index} 不存在")


class BlindAnnotationRequestHandler(BaseHTTPRequestHandler):
    server: "BlindAnnotationHTTPServer"

    @property
    def application(self) -> BlindAnnotationApplication:
        return self.server.application

    def do_GET(self) -> None:
        self._handle(self._dispatch_get)

    def do_POST(self) -> None:
        self._handle(self._dispatch_post)

    def _handle(self, callback) -> None:
        try:
            callback()
        except FileNotFoundError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except KeyError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except ConnectionError:
            # Browsers routinely cancel an outstanding WAV range when the user
            # seeks, reloads, or switches windows. The response is already gone.
            return
        except Exception as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"盲标服务错误：{exc}"},
            )

    def _dispatch_get(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/" and self._consume_token(parsed):
            return
        if not self._authenticated():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "请使用命令输出的安全链接"})
            return
        if parsed.path == "/":
            self._send_asset("blind.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/assets/blind.js":
            self._send_asset("blind.js", "text/javascript; charset=utf-8")
            return
        if parsed.path == "/assets/blind.css":
            self._send_asset("blind.css", "text/css; charset=utf-8")
            return
        if parsed.path == "/api/task":
            self._send_json(HTTPStatus.OK, self.application.task_payload())
            return
        audio_match = _match_path(parsed.path, r"/audio/(?P<window_index>\d+)")
        if audio_match:
            self._send_audio(
                self.application.audio_path(int(audio_match["window_index"]))
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "页面不存在"})

    def _dispatch_post(self) -> None:
        if not self._authorized_mutation():
            return
        parsed = urlparse(self.path)
        body = self._read_json()
        if parsed.path == "/api/utterances":
            self._send_json(
                HTTPStatus.OK,
                {"utterance": self.application.save_utterance(body)},
            )
            return
        delete_match = _match_path(
            parsed.path, r"/api/utterances/(?P<utterance_id>[A-Za-z0-9-]+)/delete"
        )
        if delete_match:
            self.application.delete_utterance(unquote(delete_match["utterance_id"]))
            self._send_json(HTTPStatus.OK, {"deleted": True})
            return
        status_match = _match_path(
            parsed.path, r"/api/windows/(?P<window_index>\d+)/status"
        )
        if status_match:
            window = self.application.set_window_status(
                int(status_match["window_index"]), str(body.get("status") or "")
            )
            self._send_json(HTTPStatus.OK, {"window": window})
            return
        if parsed.path == "/api/finalize":
            task = self.application.finalize(
                str(body.get("annotator") or ""),
                confirm_unseen=body.get("confirm_unseen") is True,
            )
            self._send_json(HTTPStatus.OK, task)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})

    def _consume_token(self, parsed) -> bool:
        token = parse_qs(parsed.query).get("token", [None])[0]
        if token is None:
            return False
        if not secrets.compare_digest(token, self.application.token):
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "安全链接已失效"})
            return True
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.send_header(
            "Set-Cookie",
            f"allday_blind_session={self.application.token}; Path=/; HttpOnly; SameSite=Strict",
        )
        self._security_headers()
        self.end_headers()
        return True

    def _authenticated(self) -> bool:
        header_token = self.headers.get("X-AllDay-Token")
        if header_token and secrets.compare_digest(header_token, self.application.token):
            return True
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        session = cookie.get("allday_blind_session")
        return bool(
            session and secrets.compare_digest(session.value, self.application.token)
        )

    def _authorized_mutation(self) -> bool:
        if not self._authenticated():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "未授权"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {
            self.application.base_url,
            f"http://localhost:{self.application.port}",
        }:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "拒绝跨站请求"})
            return False
        return True

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if length <= 0 or length > MAX_JSON_BODY:
            raise ValueError("请求正文大小无效")
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ValueError("请求正文不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("请求正文必须是 JSON 对象")
        return payload

    def _send_asset(self, filename: str, content_type: str) -> None:
        path = BLIND_ASSET_ROOT / filename
        if not path.is_file():
            raise FileNotFoundError(f"网页资源不存在：{filename}")
        self._send_bytes(HTTPStatus.OK, path.read_bytes(), content_type)

    def _send_audio(self, path: Path) -> None:
        size = path.stat().st_size
        start = 0
        end = size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range")
        if range_header:
            if not range_header.startswith("bytes=") or "," in range_header:
                raise ValueError("只支持单个音频 byte range")
            raw_start, _, raw_end = range_header[6:].partition("-")
            if not raw_start:
                suffix_length = int(raw_end)
                start = max(0, size - suffix_length)
            else:
                start = int(raw_start)
                end = int(raw_end) if raw_end else end
            if start < 0 or end < start or start >= size:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self._security_headers()
                self.end_headers()
                return
            end = min(end, size - 1)
            status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "private, max-age=3600")
        self._security_headers()
        self.end_headers()
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _send_json(self, status: HTTPStatus, payload: Any) -> None:
        self._send_bytes(
            status,
            json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _send_bytes(
        self, status: HTTPStatus, data: bytes, content_type: str
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
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
            "connect-src 'self'; media-src 'self'; img-src 'self'; frame-ancestors 'none'",
        )

    def log_message(self, format: str, *args) -> None:
        print(f"[blind-web] {self.address_string()} {format % args}")


class BlindAnnotationHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, application: BlindAnnotationApplication):
        self.application = application
        super().__init__(server_address, BlindAnnotationRequestHandler)


def create_blind_annotation_server(
    task_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8766,
    token: str | None = None,
) -> BlindAnnotationHTTPServer:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("盲标网页只能绑定 127.0.0.1 或 localhost")
    application = BlindAnnotationApplication(task_path, token=token)
    server = BlindAnnotationHTTPServer((host, port), application)
    bound_host, bound_port = server.server_address[:2]
    application.host = "127.0.0.1" if bound_host in {"0.0.0.0", "::"} else host
    application.port = int(bound_port)
    return server


def serve_blind_annotation(
    task_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8766,
    open_browser: bool = True,
) -> None:
    server = create_blind_annotation_server(task_path, host=host, port=port)
    url = f"{server.application.base_url}/?token={server.application.token}"
    print(f"V2-C.1 独立盲标台：{url}")
    print("页面不会读取数据库或任何模型输出；按 Ctrl+C 停止。")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _annotation_row(
    key: str,
    kind: str,
    start_ms: int,
    end_ms: int,
    *,
    label: str | None = None,
    text: str | None = None,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": "annotation",
        "key": key,
        "kind": kind,
        "session_start_ms": start_ms,
        "session_end_ms": end_ms,
        "label": label,
        "text": text,
        "metadata": metadata,
    }


def _row_utterance_id(row: dict[str, Any]) -> str:
    metadata = row.get("metadata", {})
    return str(metadata.get("utterance_id") or "") if isinstance(metadata, dict) else ""


def _required_nonnegative_int(payload: dict[str, Any], name: str) -> int:
    try:
        value = int(payload[name])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if value < 0:
        raise ValueError(f"{name} 不能小于 0")
    return value


def _match_path(path: str, pattern: str) -> dict[str, str] | None:
    import re

    match = re.fullmatch(pattern, path)
    return match.groupdict() if match else None
