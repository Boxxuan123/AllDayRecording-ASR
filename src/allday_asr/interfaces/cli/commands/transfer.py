from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from allday_asr.interfaces.transfer.server import (
    DEFAULT_PASSKEY_ORIGIN,
    DEFAULT_PASSKEY_RP_ID,
    DEFAULT_TRANSFER_PORT,
    serve_transfer,
)
from allday_asr.paths import DEFAULT_CONFIG_PATH, DEFAULT_DB_PATH, PROJECT_ROOT
from allday_asr.v3.config import DeploymentMode, V3ConfigurationError, V3Settings


DEFAULT_INBOX = PROJECT_ROOT / "data" / "phone-inbox"
DEFAULT_TLS_IDENTITY_DIR = PROJECT_ROOT / "state" / "transfer-tls"
DEFAULT_PASSKEY_STATE = PROJECT_ROOT / "state" / "transfer-passkeys.json"
DEFAULT_DEVICE_STATE = PROJECT_ROOT / "state" / "transfer-devices.json"
DEFAULT_V3_STATE_DIR = PROJECT_ROOT / "state" / "v3"

app = typer.Typer(
    help="在局域网中安全接收手机录音和会话清单。",
    no_args_is_help=True,
)


@app.command(name="receive")
def receive_command(
    host: str = typer.Option(
        "0.0.0.0",
        help="监听地址；默认接受本机所有 IPv4 网卡的连接。",
    ),
    port: int = typer.Option(
        DEFAULT_TRANSFER_PORT,
        min=1,
        max=65535,
        help="手机文件接收端口。",
    ),
    inbox: Path = typer.Option(
        DEFAULT_INBOX,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="完成校验后的文件接收目录。",
    ),
    token: Optional[str] = typer.Option(
        None,
        envvar="ALLDAY_TRANSFER_TOKEN",
        help="固定首次配对码；只用于登记 Passkey，不能用于传输。",
    ),
    tls_cert: Optional[Path] = typer.Option(
        None,
        exists=True,
        file_okay=True,
        dir_okay=False,
        resolve_path=True,
        help="可选的 PEM TLS 证书；必须与 --tls-key 同时使用。",
    ),
    tls_key: Optional[Path] = typer.Option(
        None,
        exists=True,
        file_okay=True,
        dir_okay=False,
        resolve_path=True,
        help="可选的 PEM TLS 私钥；必须与 --tls-cert 同时使用。",
    ),
    tls_identity_dir: Path = typer.Option(
        DEFAULT_TLS_IDENTITY_DIR,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="自动生成并持久保存本地 TLS 配对身份的目录。",
    ),
    insecure_http: bool = typer.Option(
        False,
        "--insecure-http",
        help="仅供本机协议调试；明文传输不能用于真实录音。",
    ),
    passkey_state: Path = typer.Option(
        DEFAULT_PASSKEY_STATE,
        file_okay=True,
        dir_okay=False,
        resolve_path=True,
        help="持久保存 Passkey 公钥和签名计数器的 JSON 文件。",
    ),
    device_state: Path = typer.Option(
        DEFAULT_DEVICE_STATE,
        file_okay=True,
        dir_okay=False,
        resolve_path=True,
        help="持久保存已授权手机设备公钥的 JSON 文件。",
    ),
    passkey_rp_id: str = typer.Option(
        DEFAULT_PASSKEY_RP_ID,
        help="稳定的 Passkey RP ID；已配对后不可随意更改。",
    ),
    passkey_origin: str = typer.Option(
        DEFAULT_PASSKEY_ORIGIN,
        help="手机 Passkey API 返回的可信 origin。",
    ),
    auto_workflow: bool = typer.Option(
        False,
        "--auto-workflow",
        help="每个会话 manifest 完整上传后，后台自动导入并串行启动 V2。",
    ),
    workflow_shadow: bool = typer.Option(
        False,
        "--workflow-shadow",
        help="明确允许自动 V2 在没有生产级独立备份时以 shadow 模式运行。",
    ),
    workflow_backup_root: Optional[Path] = typer.Option(
        None,
        "--workflow-backup-root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="production 自动 V2 在启动模型前写入并验证的独立备份根目录。",
    ),
    workflow_backup_storage_kind: str = typer.Option(
        "independent_device",
        "--workflow-backup-storage-kind",
        help="自动备份类型：independent_device 或 network。",
    ),
    workflow_profile: Optional[str] = typer.Option(
        None,
        "--workflow-profile",
        help="自动 V2 的显存档位：auto、quality-16gb 或 compatible-8gb。",
    ),
    workflow_diarization_model_path: Optional[Path] = typer.Option(
        None,
        "--workflow-diarization-model-path",
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="自动 V2 使用的已下载 Community-1 本地 snapshot。",
    ),
    workflow_config: Path = typer.Option(
        DEFAULT_CONFIG_PATH,
        "--workflow-config",
        exists=True,
        file_okay=True,
        dir_okay=False,
        resolve_path=True,
        help="自动 V2 使用的 TOML 配置文件。",
    ),
    workflow_db: Path = typer.Option(
        DEFAULT_DB_PATH,
        "--workflow-db",
        help="自动导入和 V2 工作流使用的 SQLite 数据库。",
    ),
    enable_v3: bool = typer.Option(
        True,
        "--v3/--legacy-only",
        help="默认启用 V3 Device API；仅回退诊断时才使用 --legacy-only。",
    ),
    v3_state_dir: Path = typer.Option(
        DEFAULT_V3_STATE_DIR,
        "--v3-state-dir",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="V3 Core 独立数据库、音频和制品存储目录。",
    ),
) -> None:
    """启动独立于本机网页工作台的可断点续传接收服务。"""
    try:
        if enable_v3:
            settings = V3Settings.from_environment()
            if not settings.enabled:
                raise V3ConfigurationError(
                    "V3 Device API 已被 ALLDAY_V3_ENABLED=0 显式禁用"
                )
            if settings.deployment_mode is DeploymentMode.PRODUCTION and (
                passkey_rp_id != settings.passkey_rp_id
                or passkey_origin not in settings.passkey_origins
            ):
                raise V3ConfigurationError(
                    "production Device listener 的 RP ID/origin 必须与发布环境完全一致"
                )
        serve_transfer(
            inbox=inbox,
            host=host,
            port=port,
            token=token,
            tls_cert=tls_cert,
            tls_key=tls_key,
            tls_identity_dir=tls_identity_dir,
            insecure_http=insecure_http,
            passkey_state=passkey_state,
            device_state=device_state,
            passkey_rp_id=passkey_rp_id,
            passkey_origins=(passkey_origin,),
            auto_workflow=auto_workflow,
            workflow_shadow=workflow_shadow,
            workflow_config=workflow_config,
            workflow_database=workflow_db,
            workflow_profile=workflow_profile,
            workflow_diarization_model_path=workflow_diarization_model_path,
            workflow_backup_root=workflow_backup_root,
            workflow_backup_storage_kind=workflow_backup_storage_kind,
            enable_v3=enable_v3,
            v3_state_dir=v3_state_dir,
        )
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
