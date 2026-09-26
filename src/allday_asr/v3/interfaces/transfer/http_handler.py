from __future__ import annotations

import json
import secrets
import logging
import re
import time
import traceback
from pathlib import Path
from collections.abc import Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import urlparse
from allday_asr.v3.domain.ids import new_ulid

from .devices import (
    DEVICE_CHALLENGE_HEADER,
    DEVICE_ID_HEADER,
    DEVICE_SIGNATURE_HEADER,
    DeviceAuthError,
    DeviceConflictError,
    DeviceForbiddenError,
    DeviceUnauthorizedError,
)
from .passkeys import (
    PASSKEY_ASSERTION_HEADER,
    PASSKEY_CEREMONY_HEADER,
    PasskeyConflictError,
    PasskeyError,
    PasskeyUnauthorizedError,
    RequestBinding,
)
from .protocol import (
    MAX_JSON_BODY_BYTES,
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    _AUTHENTICATE_OPTIONS_PATH,
    _DEVICE_CHALLENGE_PATH,
    _REGISTER_OPTIONS_PATH,
    _REGISTER_VERIFY_PATH,
    _UPLOAD_PATH,
    _V3_STATUS_PATH,
    _V3_SYNC_PATH,
)
from .http_review_routes import dispatch_review_get, dispatch_review_post
from .store import (
    UploadConflictError,
    UploadDigestError,
    UploadNotFoundError,
    UploadOffsetError,
    UploadStoreError,
)

logger = logging.getLogger(__name__)


def safe_exception_stack(exc: BaseException) -> str:
    """Keep frame/function/line and cause types, never payloads, locals or paths."""
    parts = []
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        parts.append(type(exc).__name__)
        for frame in traceback.extract_tb(exc.__traceback__):
            parts.append(f'{Path(frame.filename).name}:{frame.name}:{frame.lineno}')
        exc = exc.__cause__ or exc.__context__
    return ' <- '.join(parts)


class TransferRequestHandler(BaseHTTPRequestHandler):
    server: Any
    server_version = "AllDayRecordingTransfer/2"
    sys_version = ""
    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(60)
    def do_GET(self) -> None:
        self._handle(self._dispatch_get)
    def do_POST(self) -> None:
        self._handle(self._dispatch_post)
    def do_PUT(self) -> None:
        self._handle(self._dispatch_put)

    def _handle(self, callback) -> None:
        candidate = self.headers.get('XAllDayRequestId', self.headers.get('X-AllDay-Request-ID', ''))
        self.request_id = new_ulid()
        self.client_request_id = candidate if re.fullmatch(r'[A-Za-z0-9-]{1,80}', candidate) else ''
        started = time.monotonic()
        self.response_status = 0
        # Paths can contain resource IDs; redact dynamic suffixes and query strings.
        path = urlparse(self.path).path
        known = {_V3_STATUS_PATH, _V3_SYNC_PATH, _DEVICE_CHALLENGE_PATH,
                 _REGISTER_OPTIONS_PATH, _REGISTER_VERIFY_PATH, _AUTHENTICATE_OPTIONS_PATH,
                 '/device/v3/reviews', '/device/v3/reviews/action', '/device/v3/reviews/audio',
                 '/device/v3/annotations', '/api/v1/status', '/api/v1/uploads'}
        self.diagnostic_path = path if path in known else (
            '/api/v1/uploads/:id' if _UPLOAD_PATH.fullmatch(path) else '/other')
        logger.debug('transfer id=%s client_id=%s phase=request start method=%s path=%s',
                    self.request_id, self.client_request_id, self.command, self.diagnostic_path)
        try:
            callback()
        except UploadNotFoundError as exc:
            self._send_error(HTTPStatus.NOT_FOUND, str(exc))
        except UploadOffsetError as exc:
            self._send_json(
                HTTPStatus.CONFLICT,
                {
                    "error": str(exc),
                    "expected_offset": exc.expected_offset,
                },
                headers={"Upload-Offset": str(exc.expected_offset)},
            )
        except UploadConflictError as exc:
            self._send_error(HTTPStatus.CONFLICT, str(exc))
        except UploadDigestError as exc:
            self._send_json(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                {
                    "error": str(exc),
                    "expected_sha256": exc.expected_digest,
                    "actual_sha256": exc.actual_digest,
                    "expected_offset": 0,
                },
                headers={"Upload-Offset": "0"},
            )
        except UploadStoreError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except DeviceUnauthorizedError as exc:
            self._send_error(
                HTTPStatus.UNAUTHORIZED,
                str(exc),
                headers={
                    "WWW-Authenticate": 'DeviceSignature realm="AllDayRecording Transfer"'
                },
            )
        except DeviceConflictError as exc:
            self._send_error(HTTPStatus.CONFLICT, str(exc))
        except DeviceForbiddenError as exc:
            self._send_error(HTTPStatus.FORBIDDEN, str(exc))
        except DeviceAuthError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except PasskeyUnauthorizedError as exc:
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {"error": str(exc)},
                headers={"WWW-Authenticate": 'Passkey realm="AllDayRecording Transfer"'},
            )
        except PasskeyConflictError as exc:
            self._send_error(HTTPStatus.CONFLICT, str(exc))
        except PasskeyError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except Exception as exc:
            logger.error('transfer id=%s category=internal stack=%s', self.request_id, safe_exception_stack(exc))
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "接收服务内部错误",
            )
        finally:
            log = logger.warning if self.response_status >= 400 or self.response_status == 0 else logger.debug
            log('transfer id=%s client_id=%s method=%s path=%s phase=response end status=%s ms=%.1f',
                self.request_id, self.client_request_id, self.command, self.diagnostic_path,
                self.response_status, (time.monotonic() - started) * 1000)

    def _dispatch_get(self) -> None:
        path = urlparse(self.path).path
        if path == _V3_STATUS_PATH:
            if self.server.v3_gateway is None:
                self._send_error(HTTPStatus.NOT_FOUND, "接口不存在")
                return
            binding = RequestBinding.for_request(method="GET", path=path, body=b"")
            device = self._authenticate_device_request(binding)
            self._send_json(
                HTTPStatus.OK,
                self.server.v3_gateway.status(device.device_id),
            )
            return
        if dispatch_review_get(self, path):
            return
        match = _UPLOAD_PATH.fullmatch(path)
        if path != "/api/v1/status" and match is None:
            self._send_error(HTTPStatus.NOT_FOUND, "接口不存在")
            return
        binding = RequestBinding.for_request(
            method="GET",
            path=path,
            body=b"",
        )
        self._authenticate_request(binding)
        if path == "/api/v1/status":
            self._send_json(
                HTTPStatus.OK,
                {
                    "protocol": PROTOCOL_NAME,
                    "version": PROTOCOL_VERSION,
                    "status": "ready",
                    "transport": (
                        "tls" if self.server.tls_enabled else "insecure-http"
                    ),
                    "max_file_bytes": self.server.store.max_file_bytes,
                    "max_chunk_bytes": self.server.store.max_chunk_bytes,
                    "allowed_kinds": ["recording", "manifest"],
                    "authentication": "device-signature",
                    "enrollment": "passkey",
                    "passkey_rp_id": self.server.passkeys.store.rp_id,
                    "receiver_id": self.server.receiver_id,
                },
            )
            return
        if match:
            record = self.server.store.get_upload(match.group("upload_id"))
            self._send_upload_record(HTTPStatus.OK, record.public_dict())
            return

    def _dispatch_post(self) -> None:
        path = urlparse(self.path).path
        if path == _REGISTER_OPTIONS_PATH:
            if not self._authenticate_pairing_code():
                return
            body = self._read_json()
            if set(body) != {"device_name"}:
                raise PasskeyError("登记选项只接受 device_name 字段")
            result = self.server.passkeys.start_registration(
                device_name=body["device_name"],
            )
            self._send_json(HTTPStatus.OK, result)
            return
        if path == _REGISTER_VERIFY_PATH:
            if not self._authenticate_pairing_code():
                return
            body = self._read_json()
            if set(body) not in (
                {"ceremony_id", "credential"},
                {"ceremony_id", "credential", "device"},
            ):
                raise PasskeyError(
                    "登记验证只接受 ceremony_id、credential 和可选 device 字段"
                )
            credential = body["credential"]
            if not isinstance(credential, dict):
                raise PasskeyError("credential 必须是 JSON 对象")
            record = self.server.passkeys.finish_registration(
                ceremony_id=body["ceremony_id"],
                credential=credential,
            )
            device_record = None
            if "device" in body:
                device = body["device"]
                if not isinstance(device, dict) or set(device) != {
                    "algorithm",
                    "public_key",
                }:
                    raise DeviceAuthError(
                        "device 必须包含 algorithm 和 public_key"
                    )
                device_record = self.server.devices.store.register(
                    device_name=record.device_name,
                    algorithm=device["algorithm"],
                    public_key=device["public_key"],
                    passkey_credential_id=record.credential_id,
                )
                if self.server.v3_gateway is not None:
                    self.server.v3_gateway.device_enrolled(device_record)
            self._send_json(
                HTTPStatus.CREATED,
                {
                    "credential": record.public_dict(),
                    "device": (
                        device_record.public_dict()
                        if device_record is not None
                        else None
                    ),
                    "receiver_id": self.server.receiver_id,
                },
            )
            return
        if path == _DEVICE_CHALLENGE_PATH:
            body = self._read_json()
            if set(body) != {"device_id", "request"} or not isinstance(
                body["request"], dict
            ):
                raise DeviceAuthError(
                    "设备认证 challenge 必须包含 device_id 和 request"
                )
            binding = RequestBinding.from_dict(body["request"])
            self._send_json(
                HTTPStatus.OK,
                self.server.devices.start_authentication(
                    device_id=body["device_id"],
                    binding=binding,
                ),
            )
            return
        if path == _AUTHENTICATE_OPTIONS_PATH:
            body = self._read_json()
            if set(body) != {"request"} or not isinstance(body["request"], dict):
                raise PasskeyError("认证选项必须包含 request 对象")
            binding = RequestBinding.from_dict(body["request"])
            self._send_json(
                HTTPStatus.OK,
                self.server.passkeys.start_authentication(binding),
            )
            return
        if path == _V3_SYNC_PATH:
            if self.server.v3_gateway is None:
                self._send_error(HTTPStatus.NOT_FOUND, "接口不存在")
                return
            raw = self._read_body(max_bytes=MAX_JSON_BODY_BYTES)
            binding = RequestBinding.for_request(
                method="POST",
                path=path,
                body=raw,
            )
            device = self._authenticate_device_request(binding)
            try:
                response = self.server.v3_gateway.synchronize(
                    device.device_id,
                    self._decode_json(raw),
                )
            except ValueError as exc:
                raise UploadStoreError(str(exc)) from exc
            self._send_json(HTTPStatus.OK, response)
            return
        if dispatch_review_post(self, path):
            return
        if path != "/api/v1/uploads":
            self._send_error(HTTPStatus.NOT_FOUND, "接口不存在")
            return
        raw = self._read_body(max_bytes=MAX_JSON_BODY_BYTES)
        binding = RequestBinding.for_request(
            method="POST",
            path=path,
            body=raw,
        )
        device = self._authenticate_request(binding)
        body = self._decode_json(raw)
        required = {"relative_path", "size", "sha256", "kind"}
        missing = sorted(required - body.keys())
        unknown = sorted(body.keys() - required)
        if missing:
            raise UploadStoreError(f"缺少字段：{', '.join(missing)}")
        if unknown:
            raise UploadStoreError(f"包含未知字段：{', '.join(unknown)}")
        record, created = self.server.store.create_upload(
            relative_path=body["relative_path"],
            size=body["size"],
            sha256=body["sha256"],
            kind=body["kind"],
        )
        automation = (
            self.server.notify_upload_completed(
                record,
                device_key_id=device.device_id if device is not None else None,
            )
            if record.kind == "manifest" and self.server.store.has_completed_file(record)
            else None
        )
        self._send_upload_record(
            HTTPStatus.CREATED if created else HTTPStatus.OK,
            record.public_dict(),
            headers={"Location": f"/api/v1/uploads/{record.upload_id}"},
            automation=automation,
        )

    def _dispatch_put(self) -> None:
        path = urlparse(self.path).path
        match = _UPLOAD_PATH.fullmatch(path)
        if not match:
            self._send_error(HTTPStatus.NOT_FOUND, "接口不存在")
            return
        offset_text = self.headers.get("Upload-Offset")
        if offset_text is None:
            raise UploadStoreError("缺少 Upload-Offset 请求头")
        try:
            offset = int(offset_text)
        except ValueError as exc:
            raise UploadStoreError("Upload-Offset 必须是非负整数") from exc
        data = self._read_body(max_bytes=self.server.store.max_chunk_bytes)
        binding = RequestBinding.for_request(
            method="PUT",
            path=path,
            body=data,
            upload_offset=offset,
        )
        device = self._authenticate_request(binding)
        record = self.server.store.append_chunk(
            match.group("upload_id"),
            offset=offset,
            data=data,
        )
        automation = self.server.notify_upload_completed(
            record,
            device_key_id=device.device_id if device is not None else None,
        )
        self._send_upload_record(
            HTTPStatus.OK,
            record.public_dict(),
            automation=automation,
        )

    def _authenticate_pairing_code(self) -> bool:
        authorization = self.headers.get("Authorization", "")
        candidate = (
            authorization.removeprefix("Bearer ")
            if authorization.startswith("Bearer ")
            else self.headers.get("X-AllDay-Transfer-Token", "")
        )
        if candidate and secrets.compare_digest(candidate, self.server.token):
            return True
        self._send_json(
            HTTPStatus.UNAUTHORIZED,
            {"error": "首次配对码无效"},
            headers={"WWW-Authenticate": 'Bearer realm="Passkey Registration"'},
        )
        return False

    def _authenticate_request(self, binding: RequestBinding):
        device_id = self.headers.get(DEVICE_ID_HEADER, "")
        if device_id:
            return self.server.devices.verify_request(
                device_id=device_id,
                challenge_id=self.headers.get(DEVICE_CHALLENGE_HEADER, ""),
                encoded_signature=self.headers.get(DEVICE_SIGNATURE_HEADER, ""),
                binding=binding,
            )
        ceremony_id = self.headers.get(PASSKEY_CEREMONY_HEADER, "")
        assertion = self.headers.get(PASSKEY_ASSERTION_HEADER, "")
        self.server.passkeys.verify_request(
            ceremony_id=ceremony_id,
            encoded_assertion=assertion,
            binding=binding,
        )
        return None

    def _authenticate_device_request(self, binding: RequestBinding):
        device_id = self.headers.get(DEVICE_ID_HEADER, "")
        if not device_id:
            raise DeviceUnauthorizedError(
                "V3 Device API 只接受已登记设备的 HUKS 签名"
            )
        return self.server.devices.verify_request(
            device_id=device_id,
            challenge_id=self.headers.get(DEVICE_CHALLENGE_HEADER, ""),
            encoded_signature=self.headers.get(DEVICE_SIGNATURE_HEADER, ""),
            binding=binding,
        )

    def _read_json(self) -> dict[str, Any]:
        raw = self._read_body(max_bytes=MAX_JSON_BODY_BYTES)
        return self._decode_json(raw)

    def _decode_json(self, raw: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UploadStoreError("请求正文不是有效的 UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise UploadStoreError("请求正文必须是 JSON 对象")
        return payload

    def _read_body(self, *, max_bytes: int) -> bytes:
        if self.headers.get("Transfer-Encoding"):
            raise UploadStoreError("不支持 Transfer-Encoding，请发送 Content-Length")
        length_text = self.headers.get("Content-Length")
        if length_text is None:
            raise UploadStoreError("缺少 Content-Length 请求头")
        try:
            length = int(length_text)
        except ValueError as exc:
            raise UploadStoreError("Content-Length 无效") from exc
        if length <= 0 or length > max_bytes:
            raise UploadStoreError(
                f"请求正文大小必须在 1 到 {max_bytes} bytes 之间"
            )
        data = self.rfile.read(length)
        if len(data) != length:
            raise UploadStoreError("请求正文未完整到达")
        return data

    def _send_upload_record(
        self,
        status: HTTPStatus,
        record: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        automation: Mapping[str, Any] | None = None,
    ) -> None:
        response_headers = {"Upload-Offset": str(record["offset"])}
        if headers:
            response_headers.update(headers)
        payload: dict[str, Any] = {"upload": record}
        if automation is not None:
            payload["automation"] = dict(automation)
        self._send_json(status, payload, headers=response_headers)

    def _send_error(
        self,
        status: HTTPStatus,
        message: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        if urlparse(self.path).path.startswith("/device/v3/"):
            self._send_json(
                status,
                {
                    "code": status.name,
                    "message": message,
                    "details": {},
                    "request_id": self.request_id,
                },
                headers=headers,
            )
            return
        self._send_json(status, {"error": message, "code": status.name,
                                 "request_id": self.request_id}, headers=headers)

    def _send_json(
        self,
        status: HTTPStatus,
        payload: Any,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.response_status = int(status)
        self.send_response(status)
        self.send_header('X-AllDay-Request-ID', self.request_id)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if headers:
            for name, value in headers.items():
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:
        # BaseHTTPRequestHandler's default access line includes raw user paths.
        return
