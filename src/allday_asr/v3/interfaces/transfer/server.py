from __future__ import annotations

import ipaddress
import hashlib
import json
import re
import secrets
import socket
import ssl
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psutil

from allday_asr.v3.interfaces.transfer.devices import (
    DEVICE_CHALLENGE_HEADER,
    DEVICE_ID_HEADER,
    DEVICE_SIGNATURE_HEADER,
    DeviceAuthError,
    DeviceAuthManager,
    DeviceConflictError,
    DeviceCredentialStore,
    DeviceForbiddenError,
    DeviceUnauthorizedError,
)
from allday_asr.v3.interfaces.transfer.discovery import (
    TransferServiceAdvertiser,
    receiver_id_from_fingerprint,
)
from allday_asr.v3.interfaces.transfer.pairing import (
    build_pairing_uri,
    write_pairing_qr,
)
from allday_asr.v3.interfaces.transfer.passkeys import (
    PASSKEY_ASSERTION_HEADER,
    PASSKEY_CEREMONY_HEADER,
    PasskeyConflictError,
    PasskeyCredentialStore,
    PasskeyError,
    PasskeyManager,
    PasskeyUnauthorizedError,
    RequestBinding,
)
from allday_asr.v3.interfaces.transfer.store import (
    DEFAULT_MAX_CHUNK_BYTES,
    DEFAULT_MAX_FILE_BYTES,
    UploadConflictError,
    UploadDigestError,
    UploadNotFoundError,
    UploadOffsetError,
    UploadRecord,
    UploadStore,
    UploadStoreError,
    KnownCompletedUpload,
)
from allday_asr.v3.interfaces.transfer.tls import (
    certificate_sha256_fingerprint,
    ensure_tls_identity,
)


PROTOCOL_NAME = "ALL_DAY_RECORDING_TRANSFER"
PROTOCOL_VERSION = 2
DEFAULT_TRANSFER_PORT = 8766
DEFAULT_PASSKEY_RP_ID = "alldayrecording.local"
DEFAULT_PASSKEY_ORIGIN = (
    "ohos:app-id:"
    "BOMBi4aSxrZhHIwywhkG+VaGo5UD0ztO9VcT8+KjMdXvlKRIwKISNRxKKfCF9zU9sT6QmwWXS/"
    "XSKT8hCt+x5hE"
)
MAX_JSON_BODY_BYTES = 64 * 1024
UploadCompletedCallback = Callable[
    [UploadRecord], Mapping[str, Any] | None
]
_UPLOAD_PATH = re.compile(r"^/api/v1/uploads/(?P<upload_id>[0-9a-f]{32})$")
_REGISTER_OPTIONS_PATH = "/api/v1/passkeys/register/options"
_REGISTER_VERIFY_PATH = "/api/v1/passkeys/register/verify"
_AUTHENTICATE_OPTIONS_PATH = "/api/v1/passkeys/authenticate/options"
_DEVICE_CHALLENGE_PATH = "/api/v1/devices/authenticate/challenge"
_V3_STATUS_PATH = "/device/v3/status"
_V3_SYNC_PATH = "/device/v3/sync"


class TransferRequestHandler(BaseHTTPRequestHandler):
    server: "TransferHTTPServer"
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
            self.log_error("unhandled transfer error: %r", exc)
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "接收服务内部错误",
            )

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
            if self.server.store.has_completed_file(record)
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
            from allday_asr.v3.domain.ids import new_ulid

            self._send_json(
                status,
                {
                    "code": status.name,
                    "message": message,
                    "details": {},
                    "request_id": new_ulid(),
                },
                headers=headers,
            )
            return
        self._send_json(status, {"error": message}, headers=headers)

    def _send_json(
        self,
        status: HTTPStatus,
        payload: Any,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
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
        print(f"[transfer] {self.address_string()} {format % args}")


class TransferHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address,
        *,
        store: UploadStore,
        token: str,
        passkeys: PasskeyManager,
        devices: DeviceAuthManager,
        receiver_id: str,
        tls_enabled: bool = False,
        tls_context: ssl.SSLContext | None = None,
        upload_completed: UploadCompletedCallback | None = None,
        v3_gateway=None,
    ) -> None:
        self.store = store
        self.token = token
        self.passkeys = passkeys
        self.devices = devices
        self.receiver_id = receiver_id_from_fingerprint(receiver_id)
        self.tls_enabled = tls_enabled
        self.tls_context = tls_context
        self.upload_completed = upload_completed
        self.v3_gateway = v3_gateway
        super().__init__(server_address, TransferRequestHandler)

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def reload_tls_identity(self, certificate: Path, private_key: Path) -> None:
        if self.tls_context is None:
            raise RuntimeError("当前接收服务没有启用 TLS")
        self.tls_context.load_cert_chain(
            certfile=str(certificate.resolve(strict=True)),
            keyfile=str(private_key.resolve(strict=True)),
        )

    def notify_upload_completed(
        self,
        record: UploadRecord,
        *,
        device_key_id: str | None = None,
    ) -> Mapping[str, Any] | None:
        if record.status != "completed":
            return None
        result: dict[str, Any] = {}
        if self.upload_completed is not None:
            try:
                callback_result = self.upload_completed(record)
                if callback_result is not None:
                    result.update(callback_result)
            except Exception as exc:
                print(
                    f"[transfer] 无法把 {record.relative_path} 加入上传后处理："
                    f"{exc!r}"
                )
                result.update({
                    "status": "queue_failed",
                    "detail": str(exc),
                })
        if self.v3_gateway is not None and device_key_id is not None:
            v3 = self.v3_gateway.upload_completed(device_key_id, record, self.store)
            if v3 is not None:
                result["v3"] = v3
        return result or None


def create_transfer_server(
    *,
    inbox: Path,
    host: str = "0.0.0.0",
    port: int = DEFAULT_TRANSFER_PORT,
    token: str | None = None,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_chunk_bytes: int = DEFAULT_MAX_CHUNK_BYTES,
    tls_cert: Path | None = None,
    tls_key: Path | None = None,
    allow_insecure_http: bool = False,
    passkey_state: Path | None = None,
    passkey_rp_id: str = DEFAULT_PASSKEY_RP_ID,
    passkey_origins: tuple[str, ...] = (DEFAULT_PASSKEY_ORIGIN,),
    passkey_manager: PasskeyManager | None = None,
    device_state: Path | None = None,
    device_manager: DeviceAuthManager | None = None,
    receiver_id: str | None = None,
    upload_completed: UploadCompletedCallback | None = None,
    v3_gateway=None,
    v3_core=None,
    v3_ingest_uploads: bool = True,
    known_completed_upload: KnownCompletedUpload | None = None,
) -> TransferHTTPServer:
    if not 0 <= port <= 65535:
        raise ValueError("端口必须在 0 到 65535 之间")
    resolved_token = token or secrets.token_urlsafe(32)
    if len(resolved_token) < 16 or resolved_token.strip() != resolved_token:
        raise ValueError("首次配对码至少需要 16 个字符，且首尾不能有空白")
    if (tls_cert is None) != (tls_key is None):
        raise ValueError("启用 TLS 时必须同时提供证书和私钥")
    if tls_cert is None and not allow_insecure_http:
        raise ValueError("接收服务拒绝明文 HTTP；必须配置 TLS")
    if tls_cert is None and host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("明文 HTTP 只允许绑定本机回环地址")

    tls_context: ssl.SSLContext | None = None
    if tls_cert is not None and tls_key is not None:
        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
        tls_context.options |= ssl.OP_NO_COMPRESSION
        tls_context.load_cert_chain(
            certfile=str(tls_cert.resolve(strict=True)),
            keyfile=str(tls_key.resolve(strict=True)),
        )

    store = UploadStore(
        inbox,
        max_file_bytes=max_file_bytes,
        max_chunk_bytes=max_chunk_bytes,
        known_completed=known_completed_upload,
    )
    if passkey_manager is None:
        registry_path = passkey_state or (
            inbox.expanduser().resolve().parent / ".transfer-passkeys.json"
        )
        passkey_manager = PasskeyManager(
            PasskeyCredentialStore(
                registry_path,
                rp_id=passkey_rp_id,
                expected_origins=passkey_origins,
            )
        )
    if device_manager is None:
        device_registry_path = device_state or (
            inbox.expanduser().resolve().parent / ".transfer-devices.json"
        )
        device_manager = DeviceAuthManager(DeviceCredentialStore(device_registry_path))
    if receiver_id is None:
        if tls_cert is not None:
            receiver_id = receiver_id_from_fingerprint(
                certificate_sha256_fingerprint(tls_cert)
            )
        else:
            receiver_id = hashlib.sha256(
                f"insecure-debug:{resolved_token}".encode("utf-8")
            ).hexdigest()
    if v3_core is not None:
        if v3_gateway is not None:
            raise ValueError("v3_core 与 v3_gateway 不能同时指定")
        from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
        from allday_asr.v3.adapters.transfer import (
            TransferDeviceTrustAdapter,
            V3UploadIngestAdapter,
        )
        from allday_asr.v3.interfaces.device_gateway import DeviceGateway

        v3_core.initialize()
        trust = TransferDeviceTrustAdapter(
            device_manager,
            lambda: SqliteUnitOfWork(v3_core.database),
            receiver_id_from_fingerprint(receiver_id),
        )
        ingest = (
            V3UploadIngestAdapter(
                trust,
                lambda: SqliteUnitOfWork(v3_core.database),
                v3_core.audio_store,
                v3_core.artifact_store,
            )
            if v3_ingest_uploads
            else None
        )
        v3_gateway = DeviceGateway(trust, v3_core.mobile_sync, ingest)
        v3_gateway.reconcile()
        device_manager = trust
    server = TransferHTTPServer(
        (host, port),
        store=store,
        token=resolved_token,
        passkeys=passkey_manager,
        devices=device_manager,
        receiver_id=receiver_id,
        tls_enabled=tls_cert is not None,
        tls_context=tls_context,
        upload_completed=upload_completed,
        v3_gateway=v3_gateway,
    )
    if tls_context is not None:
        try:
            server.socket = tls_context.wrap_socket(server.socket, server_side=True)
        except Exception:
            server.server_close()
            raise
    return server


def _known_completed_v3_uploads(core: Any) -> KnownCompletedUpload:
    """Snapshot V3-owned payload identities for receiver-wide deduplication."""

    core.initialize()
    with core.database.read() as connection:
        recordings = {
            (str(row["sha256"]).lower(), int(row["size_bytes"]))
            for row in connection.execute("SELECT sha256, size_bytes FROM audio_assets")
        }
        manifest_rows = connection.execute(
            "SELECT sha256, storage_ref FROM session_manifests"
        ).fetchall()
    manifests = {
        (
            str(row["sha256"]).lower(),
            core.artifact_store.path_for(str(row["storage_ref"])).stat().st_size,
        )
        for row in manifest_rows
    }

    def known_completed(
        relative_path: str,
        size: int,
        sha256: str,
        kind: str,
    ) -> bool:
        del relative_path
        identity = (sha256, size)
        return identity in (recordings if kind == "recording" else manifests)

    return known_completed


def serve_transfer(
    *,
    inbox: Path,
    host: str = "0.0.0.0",
    port: int = DEFAULT_TRANSFER_PORT,
    token: str | None = None,
    tls_cert: Path | None = None,
    tls_key: Path | None = None,
    tls_identity_dir: Path | None = None,
    insecure_http: bool = False,
    passkey_state: Path | None = None,
    device_state: Path | None = None,
    passkey_rp_id: str = DEFAULT_PASSKEY_RP_ID,
    passkey_origins: tuple[str, ...] = (DEFAULT_PASSKEY_ORIGIN,),
    auto_workflow: bool = False,
    workflow_shadow: bool = False,
    workflow_config: Path | None = None,
    workflow_profile: str | None = None,
    workflow_diarization_model_path: Path | None = None,
    workflow_backup_root: Path | None = None,
    workflow_backup_storage_kind: str = "independent_device",
    v3_state_dir: Path | None = None,
    v3_reasoning_effort: str = "auto",
) -> None:
    if insecure_http and (tls_cert is not None or tls_key is not None):
        raise ValueError("--insecure-http 不能与 TLS 证书参数同时使用")
    if workflow_shadow and not auto_workflow:
        raise ValueError(
            "--workflow-shadow 必须与 --auto-process "
            "（兼容别名 --auto-workflow）一起使用"
        )
    if workflow_backup_root is not None and not auto_workflow:
        raise ValueError(
            "--workflow-backup-root 必须与 --auto-process "
            "（兼容别名 --auto-workflow）一起使用"
        )
    if auto_workflow and workflow_config is None:
        raise ValueError("自动处理缺少模型配置文件")
    if auto_workflow and not workflow_shadow and workflow_backup_root is None:
        raise ValueError(
            "production 自动处理必须提供 --workflow-backup-root；"
            "若明确接受风险，请同时使用 --workflow-shadow"
        )
    if workflow_backup_root is not None and workflow_backup_storage_kind not in {
        "independent_device",
        "network",
    }:
        raise ValueError(
            "--workflow-backup-storage-kind 必须是 independent_device 或 network"
        )
    if v3_reasoning_effort not in {"auto", "low", "medium", "high", "xhigh"}:
        raise ValueError(
            "--v3-reasoning-effort 必须是 auto、low、medium、high 或 xhigh"
        )
    pairing_fingerprint: str | None = None
    trust_certificate_path: Path | None = None
    automatic_identity = False
    if not insecure_http and tls_cert is None and tls_key is None:
        if tls_identity_dir is None:
            raise ValueError("自动 TLS 需要身份保存目录")
        identity = ensure_tls_identity(
            tls_identity_dir,
            addresses=_tls_addresses(host),
        )
        tls_cert = identity.certificate_path
        tls_key = identity.private_key_path
        pairing_fingerprint = identity.ca_sha256_fingerprint
        trust_certificate_path = identity.ca_certificate_path
        automatic_identity = True
    elif tls_cert is not None:
        pairing_fingerprint = certificate_sha256_fingerprint(tls_cert)
        trust_certificate_path = tls_cert

    receiver_id = (
        receiver_id_from_fingerprint(pairing_fingerprint)
        if pairing_fingerprint is not None
        else None
    )
    if v3_state_dir is None:
        raise ValueError("V3 Device API 需要独立的 --v3-state-dir")
    from allday_asr.v3.bootstrap import V3CorePaths, compose_v3_core

    v3_core = compose_v3_core(V3CorePaths.from_state_dir(v3_state_dir))
    known_completed_upload = _known_completed_v3_uploads(v3_core)
    server = create_transfer_server(
        inbox=inbox,
        host=host,
        port=port,
        token=token,
        tls_cert=tls_cert,
        tls_key=tls_key,
        allow_insecure_http=insecure_http,
        passkey_state=passkey_state,
        device_state=device_state,
        passkey_rp_id=passkey_rp_id,
        passkey_origins=passkey_origins,
        receiver_id=receiver_id,
        v3_core=v3_core,
        v3_ingest_uploads=True,
        known_completed_upload=known_completed_upload,
    )
    automatic_runner = None
    if auto_workflow:
        from allday_asr.v3.model_config import load_model_config
        from allday_asr.v3.adapters.models import build_native_model_pipeline
        from allday_asr.v3.adapters.transfer import V3AutomaticWorkflowRunner

        try:
            model_adapter = build_native_model_pipeline(
                v3_core.database,
                v3_core.audio_store,
                load_model_config(workflow_config),
                requested_profile=workflow_profile,
                diarization_model_path=workflow_diarization_model_path,
            )
            automatic_runner = V3AutomaticWorkflowRunner(
                v3_core,
                model_adapter,
                backup_root=workflow_backup_root,
                backup_storage_kind=workflow_backup_storage_kind,
                shadow=workflow_shadow,
                reasoning_effort=v3_reasoning_effort,
            )
            if server.v3_gateway is None:
                raise RuntimeError("V3 Device Gateway was not composed")
            server.v3_gateway.session_ingested = automatic_runner.submit
        except Exception:
            server.server_close()
            if automatic_runner is not None:
                automatic_runner.close()
            raise
    scheme = "https" if server.tls_enabled else "http"
    pairing_qr_path: Path | None = None
    advertiser: TransferServiceAdvertiser | None = None
    if (
        server.tls_enabled
        and pairing_fingerprint is not None
        and trust_certificate_path is not None
    ):
        qr_root = (
            tls_identity_dir
            if tls_identity_dir is not None
            else inbox.expanduser().resolve().parent
        )
        pairing_qr_path = qr_root / "transfer-pairing.png"

        def update_pairing_artifacts(addresses: tuple[str, ...]) -> None:
            if automatic_identity:
                refreshed = ensure_tls_identity(
                    tls_identity_dir,
                    addresses=addresses,
                )
                server.reload_tls_identity(
                    refreshed.certificate_path,
                    refreshed.private_key_path,
                )
            pairing_uri = build_pairing_uri(
                receiver_id=server.receiver_id,
                addresses=[f"https://{value}:{server.port}" for value in addresses],
                ca_fingerprint=pairing_fingerprint,
                ca_pem=trust_certificate_path.read_text(encoding="utf-8"),
                pairing_code=server.token,
            )
            write_pairing_qr(pairing_qr_path, pairing_uri)

        current_addresses = tuple(_tls_addresses(host))
        if current_addresses:
            update_pairing_artifacts(current_addresses)
        advertiser = TransferServiceAdvertiser(
            receiver_id=server.receiver_id,
            port=server.port,
            address_provider=lambda: _tls_addresses(host),
            before_address_update=update_pairing_artifacts,
        )
        advertiser.start()
    print("AllDayRecording 手机文件接收服务已启动")
    for address in _display_addresses(host):
        print(f"接收地址：{scheme}://{address}:{server.port}")
    print(f"首次配对码（仅可登记 Passkey）：{server.token}")
    if pairing_fingerprint is not None and trust_certificate_path is not None:
        print(f"配对证书：{trust_certificate_path}")
        print(f"配对证书 SHA-256：{pairing_fingerprint}")
        if pairing_qr_path is not None:
            print(f"首次配对二维码：{pairing_qr_path}")
        print(f"稳定接收端标识：{server.receiver_id}")
    print(f"接收目录：{server.store.root}")
    print(f"Passkey RP ID：{server.passkeys.store.rp_id}")
    print(f"Passkey 凭据库：{server.passkeys.store.path}")
    print(f"设备公钥库：{server.devices.store.path}")
    if automatic_runner is not None:
        print(_automatic_workflow_startup_message(shadow=workflow_shadow))
        if workflow_backup_root is not None:
            print(
                f"自动会话备份：{workflow_backup_root} "
                f"({workflow_backup_storage_kind})"
            )
    if server.tls_enabled:
        print(
            "Windows 提示：若要在标记为“公用网络”的 Wi-Fi 使用，"
            "首次防火墙授权时也需允许公用网络；无需开放路由器公网端口。"
        )
    if not server.tls_enabled:
        print("警告：已显式启用明文 HTTP，仅允许本机协议调试，不能传真实录音。")
    print("按 Ctrl+C 停止；停止后未完成文件可继续断点续传。")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        if advertiser is not None:
            advertiser.stop()
        server.server_close()
        if pairing_qr_path is not None:
            pairing_qr_path.unlink(missing_ok=True)
        if automatic_runner is not None:
            print("正在等待已经排队的自动 V3 工作流安全结束……")
            automatic_runner.close()


def _automatic_workflow_startup_message(*, shadow: bool) -> str:
    if shadow:
        mode = "shadow 非生产模式；允许模型执行，但不会解除独立备份准入阻塞"
    else:
        mode = "production；独立备份写入与回读校验通过后执行"
    return f"自动 V3 原生工作流：已启用（{mode}；manifest 完成后串行执行）"


def _display_addresses(host: str) -> list[str]:
    if host not in {"0.0.0.0", "::", ""}:
        return [host]
    interface_addresses = _active_physical_ipv4_addresses()
    if interface_addresses:
        return interface_addresses
    addresses: set[str] = set()
    try:
        for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(result[4][0])
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            addresses.add(probe.getsockname()[0])
    except OSError:
        pass
    usable = sorted(
        address
        for address in addresses
        if not ipaddress.ip_address(address).is_loopback
        and not ipaddress.ip_address(address).is_link_local
        and not ipaddress.ip_address(address).is_unspecified
    )
    return usable or ["<电脑局域网 IP>"]


_VIRTUAL_INTERFACE_MARKERS = (
    "clash",
    "docker",
    "hamachi",
    "hyper-v",
    "loopback",
    "tailscale",
    "tap",
    "tun",
    "vbox",
    "vethernet",
    "virtualbox",
    "vmware",
    "vpn",
    "wsl",
    "zerotier",
)


def _active_physical_ipv4_addresses() -> list[str]:
    """Return active LAN addresses without VPN and VM host-only adapters."""
    stats = psutil.net_if_stats()
    candidates: list[tuple[str, str]] = []
    for interface_name, records in psutil.net_if_addrs().items():
        interface_stats = stats.get(interface_name)
        if interface_stats is None or not interface_stats.isup:
            continue
        for record in records:
            if record.family == socket.AF_INET:
                candidates.append((interface_name, record.address))
    return _prioritize_interface_addresses(candidates)


def _prioritize_interface_addresses(
    candidates: list[tuple[str, str]],
) -> list[str]:
    physical: set[str] = set()
    virtual: set[str] = set()
    for interface_name, value in candidates:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        if (
            address.version != 4
            or address.is_loopback
            or address.is_link_local
            or address.is_unspecified
        ):
            continue
        normalized_name = interface_name.casefold()
        target = (
            virtual
            if any(marker in normalized_name for marker in _VIRTUAL_INTERFACE_MARKERS)
            else physical
        )
        target.add(str(address))
    # A machine that only has a VPN/virtual adapter should remain usable, but
    # those addresses must never outrank an active Wi-Fi or Ethernet adapter.
    return sorted(physical or virtual)


def _tls_addresses(host: str) -> list[str]:
    displayed = _display_addresses(host)
    return [value for value in displayed if not value.startswith("<")]
