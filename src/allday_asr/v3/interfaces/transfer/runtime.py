from __future__ import annotations

from pathlib import Path
from typing import Any

from .discovery import TransferServiceAdvertiser, receiver_id_from_fingerprint
from .network import _display_addresses, _tls_addresses
from .pairing import build_pairing_uri, write_pairing_qr
from .protocol import DEFAULT_PASSKEY_ORIGIN, DEFAULT_PASSKEY_RP_ID, DEFAULT_TRANSFER_PORT
from .composition import create_transfer_server
from .store import KnownCompletedUpload
from .tls import certificate_sha256_fingerprint, ensure_tls_identity


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
