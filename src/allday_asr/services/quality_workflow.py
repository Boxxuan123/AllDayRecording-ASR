from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from allday_asr.services.quality_asr import QualityAsrSettings, run_quality_asr
from allday_asr.services.quality_diarization import (
    QualityDiarizationSettings,
    run_quality_diarization,
)
from allday_asr.services.semantic_v2e02 import (
    SemanticV2E02Settings,
    run_semantic_v2e02,
)
from allday_asr.services.session_readiness import evaluate_session_readiness
from allday_asr.storage.database import Database


ProgressCallback = Callable[[str, str], None]
BackendFactory = Callable[[], Any]


@dataclass(frozen=True)
class QualityWorkflowSummary:
    workflow_run_id: int
    session_id: int
    recording_id: int | None
    state: str
    asr_run_id: int
    diarization_run_id: int
    semantic_run_id: int | None
    reused_stages: tuple[str, ...]


def run_quality_workflow(
    database: Database,
    recording_id: int | None,
    *,
    session_id: int,
    asr_settings: QualityAsrSettings,
    diarization_settings: QualityDiarizationSettings,
    semantic_settings: SemanticV2E02Settings,
    primary_factory: BackendFactory,
    secondary_factory: BackendFactory,
    diarization_factory: BackendFactory,
    admission_mode: str = "production",
    progress: ProgressCallback | None = None,
) -> QualityWorkflowSummary:
    """Persistently orchestrate the V2 quality stages without invoking a cloud LLM."""
    if admission_mode not in {"production", "shadow"}:
        raise ValueError("admission_mode 必须是 production 或 shadow")
    session = database.get_recording_session(session_id)
    if (
        recording_id is not None
        and session["legacy_recording_id"] is not None
        and int(session["legacy_recording_id"]) != recording_id
    ):
        raise ValueError("recording_id 与 session_id 不属于同一会话")
    config = {
        "workflow_revision": "v2-session-quality-workflow-v1",
        "admission_mode": admission_mode,
        "asr": asr_settings.to_dict(),
        "diarization": diarization_settings.to_dict(),
        "semantic": asdict(semantic_settings),
        "cloud_provider_enabled": False,
    }
    workflow_run_id = database.start_processing_run(
        recording_id,
        session_id=session_id,
        run_kind="quality_workflow_v2",
        config=config,
        config_sha256=_sha256_mapping(config),
        model_manifest={
            "orchestrator": "v2-session-quality-workflow-v1",
            "cloud_provider": None,
        },
        pipeline_version="v2-workflow.0",
    )
    state: dict[str, Any] = {
        "workflow_state": "admitted",
        "session_id": session_id,
        "admission_mode": admission_mode,
        "stages": {},
    }
    database.update_processing_run_progress(workflow_run_id, state)
    reused: list[str] = []

    def report(stage: str, detail: str) -> None:
        state["workflow_state"] = stage
        state["detail"] = detail
        database.update_processing_run_progress(workflow_run_id, state)
        if progress is not None:
            progress(stage, detail)

    try:
        report("admission_verifying", f"检查 {admission_mode} 会话准入条件")
        readiness = evaluate_session_readiness(
            database,
            session_id,
            verify_backups=admission_mode == "production",
        )
        state["readiness"] = {
            "state": readiness["state"],
            "production_backup_id": readiness["production_backup_id"],
        }
        admitted = (
            bool(readiness["production_ready"])
            if admission_mode == "production"
            else bool(readiness["shadow_ready"])
        )
        if not admitted:
            reasons = (
                readiness["blocking_reasons"]
                + (
                    readiness["production_blockers"]
                    if admission_mode == "production"
                    else []
                )
            )
            raise RuntimeError(
                f"会话未达到 {admission_mode} 准入条件：" + "；".join(reasons)
            )
        report("integrity_verifying", "重新校验清单和每个原始音频实例")
        integrity = readiness["integrity"]
        state["integrity"] = integrity
        failures = [
            item
            for item in integrity["instances"]
            if item["status"] != "verified"
        ]
        if integrity["manifest"]["status"] not in {"verified", "not_applicable"}:
            failures.append(integrity["manifest"])
        if failures:
            raise RuntimeError(
                f"原始输入完整性校验失败，共 {len(failures)} 项；不会启动模型"
            )
        if integrity["gaps"] or integrity["overlaps"]:
            raise RuntimeError(
                "会话包含 gap 或 overlap；当前质量工作流拒绝猜测缺失时间或重叠来源"
            )
        report("integrity_verified", "原始输入、清单和连续时间映射均已验证")

        report("asr_running", "检查或运行 V2-C 双模型 ASR")
        asr_run = _matching_asr_run(database, session_id, asr_settings)
        if asr_run is not None and str(asr_run["status"]) == "completed":
            asr_run_id = int(asr_run["id"])
            reused.append("asr")
        else:
            resume_run_id = int(asr_run["id"]) if asr_run is not None else None
            asr_summary = run_quality_asr(
                database,
                recording_id,
                session_id=session_id,
                settings=asr_settings,
                primary_factory=primary_factory,
                secondary_factory=secondary_factory,
                resume_run_id=resume_run_id,
            )
            asr_run_id = asr_summary.run_id
        state["stages"]["asr"] = {
            "run_id": asr_run_id,
            "status": "completed",
            "reused": "asr" in reused,
        }

        report("diarization_running", "检查或运行 V2-D 重叠说话人时间轴")
        diarization_run = _matching_diarization_run(
            database,
            session_id,
            asr_run_id=asr_run_id,
            settings=diarization_settings,
        )
        if diarization_run is not None:
            diarization_run_id = int(diarization_run["id"])
            reused.append("diarization")
        else:
            diarization_summary = run_quality_diarization(
                database,
                recording_id,
                session_id=session_id,
                asr_run_id=asr_run_id,
                settings=diarization_settings,
                backend_factory=diarization_factory,
            )
            diarization_run_id = diarization_summary.run_id
        state["stages"]["diarization"] = {
            "run_id": diarization_run_id,
            "status": "completed",
            "reused": "diarization" in reused,
        }

        committed_tokens = database.list_committed_asr_tokens(asr_run_id)
        semantic_run_id: int | None = None
        if committed_tokens:
            report("semantic_running", "检查或生成 V2-E.0.2 本地语义证据")
            semantic_run = _matching_semantic_run(
                database,
                session_id,
                asr_run_id=asr_run_id,
                diarization_run_id=diarization_run_id,
                settings=semantic_settings,
            )
            if semantic_run is not None:
                semantic_run_id = int(semantic_run["id"])
                reused.append("semantic")
            else:
                semantic_summary = run_semantic_v2e02(
                    database,
                    recording_id,
                    session_id=session_id,
                    asr_run_id=asr_run_id,
                    diarization_run_id=diarization_run_id,
                    settings=semantic_settings,
                )
                semantic_run_id = semantic_summary.run_id
            state["stages"]["semantic"] = {
                "run_id": semantic_run_id,
                "status": "completed",
                "reused": "semantic" in reused,
            }
            final_state = "semantic_ready"
        else:
            state["stages"]["semantic"] = {
                "run_id": None,
                "status": "skipped",
                "reason": "no_committed_asr_tokens",
            }
            final_state = "semantic_ready_empty"

        state["workflow_state"] = final_state
        state["detail"] = "本地 V2 证据链已完成；未调用云端 LLM"
        state["reused_stages"] = reused
        database.finish_processing_run(
            workflow_run_id,
            status="completed",
            summary=state,
        )
        return QualityWorkflowSummary(
            workflow_run_id=workflow_run_id,
            session_id=session_id,
            recording_id=recording_id,
            state=final_state,
            asr_run_id=asr_run_id,
            diarization_run_id=diarization_run_id,
            semantic_run_id=semantic_run_id,
            reused_stages=tuple(reused),
        )
    except Exception as exc:
        state["workflow_state"] = "failed"
        state["detail"] = repr(exc)
        database.finish_processing_run(
            workflow_run_id,
            status="failed",
            summary=state,
            error=repr(exc),
        )
        raise


def _matching_asr_run(
    database: Database,
    session_id: int,
    settings: QualityAsrSettings,
) -> Any | None:
    if not settings.model_signature:
        return None
    fingerprint = database.session_input_fingerprint(session_id)
    matches = [
        row
        for row in database.list_session_processing_runs(session_id)
        if str(row["run_kind"]) == "quality_asr_v2c"
        and str(row["config_sha256"]) == settings.sha256()
        and str(row["input_fingerprint"]) == fingerprint
        and str(row["status"]) in {"completed", "failed", "running"}
    ]
    completed = [row for row in matches if str(row["status"]) == "completed"]
    return completed[-1] if completed else (matches[-1] if matches else None)


def _matching_diarization_run(
    database: Database,
    session_id: int,
    *,
    asr_run_id: int,
    settings: QualityDiarizationSettings,
) -> Any | None:
    if not settings.model_signature:
        return None
    expected = settings.to_dict()
    for row in reversed(database.list_session_processing_runs(session_id)):
        if (
            str(row["run_kind"]) != "quality_diarization_v2d"
            or str(row["status"]) != "completed"
        ):
            continue
        config = _json_object(row["config_json"])
        if int(config.get("asr_run_id") or 0) != asr_run_id:
            continue
        if all(config.get(key) == value for key, value in expected.items()):
            return row
    return None


def _matching_semantic_run(
    database: Database,
    session_id: int,
    *,
    asr_run_id: int,
    diarization_run_id: int,
    settings: SemanticV2E02Settings,
) -> Any | None:
    for row in reversed(database.list_session_processing_runs(session_id)):
        if (
            str(row["run_kind"]) != "semantic_v2e0"
            or str(row["status"]) != "completed"
            or str(row["pipeline_version"]) != "v2-e.0.2"
        ):
            continue
        summary = _json_object(row["summary_json"])
        config = _json_object(row["config_json"])
        if (
            int(summary.get("asr_run_id") or 0) == asr_run_id
            and int(summary.get("diarization_run_id") or 0) == diarization_run_id
            and summary.get("provider") == "local_mock"
            and all(config.get(key) == value for key, value in asdict(settings).items())
        ):
            return row
    return None


def _json_object(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(str(value))
    return parsed if isinstance(parsed, dict) else {}


def _sha256_mapping(value: dict[str, Any]) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
