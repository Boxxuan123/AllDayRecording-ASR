from __future__ import annotations

import hashlib
import secrets
import ssl
import socket
import threading
from http.server import ThreadingHTTPServer
from collections.abc import Mapping
from pathlib import Path
from typing import Any


from allday_asr.v3.interfaces.transfer.devices import (
    DeviceAuthManager,
    DeviceCredentialStore,
)
from allday_asr.v3.interfaces.transfer.discovery import (
    receiver_id_from_fingerprint,
)
from allday_asr.v3.interfaces.transfer.passkeys import (
    PasskeyCredentialStore,
    PasskeyManager,
)
from allday_asr.v3.interfaces.transfer.store import (
    DEFAULT_MAX_CHUNK_BYTES,
    DEFAULT_MAX_FILE_BYTES,
    UploadRecord,
    UploadStore,
    KnownCompletedUpload,
)
from allday_asr.v3.interfaces.transfer.tls import (
    certificate_sha256_fingerprint,
)

from .http_handler import TransferRequestHandler
from .protocol import (
    DEFAULT_PASSKEY_ORIGIN,
    DEFAULT_PASSKEY_RP_ID,
    DEFAULT_TRANSFER_PORT,
    UploadCompletedCallback,
)




class TransferHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    max_connections = 32
    handshake_timeout = 5.0

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
        self.annotation_sample_worker = None
        self._connections: dict[socket.socket, threading.Thread] = {}
        self._connection_lock = threading.Lock()
        self._closing = False
        super().__init__(server_address, TransferRequestHandler)

    @property
    def active_connection_count(self) -> int:
        with self._connection_lock:
            return len(self._connections)

    def process_request(self, request, client_address):
        # The listener is always plain TCP. No handshake or blocking capacity wait
        # is allowed in the accept loop. Bound *all* connection workers.
        with self._connection_lock:
            if self._closing or len(self._connections) >= self.max_connections:
                self.shutdown_request(request)
                return
            if self.tls_context is not None:
                request.settimeout(self.handshake_timeout)
                request = self.tls_context.wrap_socket(
                    request, server_side=True, do_handshake_on_connect=False)
            worker = threading.Thread(target=self._serve_connection,
                                      args=(request, client_address), daemon=True,
                                      name='transfer-connection')
            self._connections[request] = worker
            try:
                worker.start()
            except Exception:
                del self._connections[request]
                self.shutdown_request(request)
                raise

    def _serve_connection(self, request, client_address):
        try:
            if isinstance(request, ssl.SSLSocket):
                request.do_handshake()
            self.finish_request(request, client_address)
        except (OSError, TimeoutError):
            # An incomplete handshake is not an HTTP request or a business receipt.
            pass
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)
            with self._connection_lock:
                self._connections.pop(request, None)

    def server_close(self):
        with self._connection_lock:
            self._closing = True
            connections = list(self._connections.items())
        super().server_close()
        for connection, _ in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        for _, worker in connections:
            if worker is not threading.current_thread():
                worker.join(self.handshake_timeout + 1)
        if self.annotation_sample_worker is not None:
            self.annotation_sample_worker.close()

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
        from allday_asr.v3.interfaces.device_reviews import DeviceReviewService

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
        v3_gateway = DeviceGateway(
            trust,
            v3_core.mobile_sync,
            ingest,
            review_service=DeviceReviewService(v3_core),
        )
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
    if v3_core is not None:
        server.annotation_sample_worker = v3_core.people.sample_worker
        server.annotation_sample_worker.start()
    return server
