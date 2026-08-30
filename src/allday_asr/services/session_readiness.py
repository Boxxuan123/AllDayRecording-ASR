from __future__ import annotations

from typing import Any

from allday_asr.application.use_cases.session_integrity import (
    mapping_discontinuities,
    verify_session_inputs,
)
from allday_asr.services.session_backup import verify_session_backup
from allday_asr.storage.database import Database

__all__ = [
    "evaluate_session_readiness",
    "mapping_discontinuities",
    "verify_session_inputs",
]


VALIDATED_WHOLE_SESSION_DIARIZATION_MS = 3 * 60 * 60 * 1000


def evaluate_session_readiness(
    database: Database,
    session_id: int,
    *,
    verify_backups: bool = True,
) -> dict[str, Any]:
    """Classify a closed session for local shadow or unattended production use."""
    session = database.get_recording_session(session_id)
    integrity = verify_session_inputs(database, session_id)
    blocking_reasons: list[str] = []
    production_blockers: list[str] = []
    warnings: list[str] = []

    failed_instances = [
        item for item in integrity["instances"] if item["status"] != "verified"
    ]
    if failed_instances:
        blocking_reasons.append(
            f"{len(failed_instances)} 个原始音频实例完整性校验失败"
        )
    if integrity["manifest"]["status"] not in {"verified", "not_applicable"}:
        blocking_reasons.append("原始采集清单完整性校验失败")
    if integrity["gaps"]:
        blocking_reasons.append("会话时间轴包含未覆盖 gap")
    if integrity["overlaps"]:
        blocking_reasons.append("会话时间轴包含重叠原音映射")

    duration_ms = int(session["duration_ms"])
    within_validated_diarization_duration = (
        duration_ms <= VALIDATED_WHOLE_SESSION_DIARIZATION_MS
    )
    if not within_validated_diarization_duration:
        production_blockers.append(
            "会话超过当前已验证的 3 小时整段 V2-D 处理范围"
        )
        warnings.append("可做受监控影子运行，但应先实现分岛说话人处理")

    backup_summaries: list[dict[str, Any]] = []
    production_backup_id: int | None = None
    for backup in reversed(database.list_session_backups(session_id)):
        item = {
            "backup_id": int(backup["id"]),
            "storage_kind": str(backup["storage_kind"]),
            "status": str(backup["status"]),
            "restore_verified": backup["restore_verified_at"] is not None,
            "bytes_verified_now": False,
        }
        is_candidate = (
            item["storage_kind"] in {"independent_device", "network"}
            and item["status"] == "verified"
            and item["restore_verified"]
        )
        if is_candidate and verify_backups:
            try:
                verify_session_backup(database, int(backup["id"]))
                item["bytes_verified_now"] = True
            except Exception as exc:
                item["status"] = "failed"
                item["error"] = repr(exc)
                is_candidate = False
        elif is_candidate:
            item["bytes_verified_now"] = None
        if is_candidate and production_backup_id is None:
            production_backup_id = int(backup["id"])
        backup_summaries.append(item)

    if production_backup_id is None:
        production_blockers.append(
            "没有通过当前校验和恢复演练的独立设备/网络备份"
        )
    if blocking_reasons:
        state = "blocked"
    elif production_blockers:
        state = "shadow_ready"
    else:
        state = "production_ready"
    return {
        "session_id": session_id,
        "session_key": str(session["session_key"]),
        "state": state,
        "shadow_ready": not blocking_reasons,
        "production_ready": not blocking_reasons and not production_blockers,
        "duration_ms": duration_ms,
        "validated_whole_session_diarization_limit_ms": (
            VALIDATED_WHOLE_SESSION_DIARIZATION_MS
        ),
        "within_validated_diarization_duration": (
            within_validated_diarization_duration
        ),
        "blocking_reasons": blocking_reasons,
        "production_blockers": production_blockers,
        "warnings": warnings,
        "production_backup_id": production_backup_id,
        "backups": backup_summaries,
        "integrity": integrity,
    }
