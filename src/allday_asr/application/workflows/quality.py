from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from allday_asr.domain.hashing import canonical_json_sha256 as _sha256_mapping
from allday_asr.services.quality_asr import QualityAsrSettings
from allday_asr.services.quality_diarization import QualityDiarizationSettings
from allday_asr.services.quality_diarization_v2d1_review import (
    effective_workflow_summary,
)
from allday_asr.services.semantic_v2e02 import SemanticV2E02Settings
from allday_asr.storage.database import Database

from .quality_models import (
    QualityWorkflowSummary,
    StageResult,
    WorkflowContext,
    WorkflowStageResults,
)
from .quality_stages import (
    BackendFactory,
    inspect_identity_mining_state,
    resolve_or_run_asr,
    resolve_or_run_diarization,
    resolve_or_run_semantic,
    run_optional_identity_audit,
    run_optional_speech_recall,
    verify_admission,
    workflow_enhancement_config,
)

ProgressCallback = Callable[[str, str], None]
ProgressReporter = Callable[[str, str], None]


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
    """Persistently orchestrate V2 stages without invoking a cloud LLM."""
    _validate_request(database, recording_id, session_id, admission_mode)
    input_fingerprint = database.session_input_fingerprint(session_id)
    config = _workflow_config(
        admission_mode,
        asr_settings,
        diarization_settings,
        semantic_settings,
    )
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
    context = WorkflowContext(
        workflow_run_id=workflow_run_id,
        session_id=session_id,
        recording_id=recording_id,
        input_fingerprint=input_fingerprint,
        admission_mode=admission_mode,
    )
    state: dict[str, Any] = {
        "workflow_state": "admitted",
        "session_id": session_id,
        "admission_mode": admission_mode,
        "stages": {},
    }
    database.update_processing_run_progress(workflow_run_id, state)

    def report(stage: str, detail: str) -> None:
        state["workflow_state"] = stage
        state["detail"] = detail
        database.update_processing_run_progress(workflow_run_id, state)
        if progress is not None:
            progress(stage, detail)

    try:
        results = _run_stages(
            database,
            context,
            state=state,
            asr_settings=asr_settings,
            diarization_settings=diarization_settings,
            semantic_settings=semantic_settings,
            primary_factory=primary_factory,
            secondary_factory=secondary_factory,
            diarization_factory=diarization_factory,
            report=report,
        )
        return _complete_workflow(database, context, state, results)
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


def _run_stages(
    database: Database,
    context: WorkflowContext,
    *,
    state: dict[str, Any],
    asr_settings: QualityAsrSettings,
    diarization_settings: QualityDiarizationSettings,
    semantic_settings: SemanticV2E02Settings,
    primary_factory: BackendFactory,
    secondary_factory: BackendFactory,
    diarization_factory: BackendFactory,
    report: ProgressReporter,
) -> WorkflowStageResults:
    admission = verify_admission(database, context, report)
    state.update(admission.summary)

    asr = resolve_or_run_asr(
        database,
        context,
        settings=asr_settings,
        primary_factory=primary_factory,
        secondary_factory=secondary_factory,
        report=report,
    )
    asr_run_id = _required_run_id(asr)
    state["stages"]["asr"] = dict(asr.summary)

    diarization = resolve_or_run_diarization(
        database,
        context,
        asr_run_id=asr_run_id,
        settings=diarization_settings,
        backend_factory=diarization_factory,
        report=report,
    )
    diarization_run_id = _required_run_id(diarization)
    state["stages"]["diarization"] = dict(diarization.summary)

    committed_tokens = database.list_committed_asr_tokens(asr_run_id)
    speech_recall = run_optional_speech_recall(
        database,
        context,
        asr_run_id=asr_run_id,
        diarization_run_id=diarization_run_id,
        report=report,
    )
    identity_audit = run_optional_identity_audit(
        database,
        context,
        diarization_run_id=diarization_run_id,
        report=report,
    )
    identity_mining = inspect_identity_mining_state(
        database,
        context,
        diarization_run_id=diarization_run_id,
        identity_audit=identity_audit,
    )
    state["enhancements"] = {
        "v2d1": dict(speech_recall.summary),
        "v2d2": dict(identity_audit.summary),
        "v2d3": dict(identity_mining.summary),
    }

    semantic = resolve_or_run_semantic(
        database,
        context,
        asr_run_id=asr_run_id,
        diarization_run_id=diarization_run_id,
        committed_tokens=committed_tokens,
        settings=semantic_settings,
        report=report,
    )
    state["stages"]["semantic"] = dict(semantic.summary)
    return WorkflowStageResults(
        admission=admission,
        asr=asr,
        diarization=diarization,
        speech_recall=speech_recall,
        identity_audit=identity_audit,
        identity_mining=identity_mining,
        semantic=semantic,
    )


def _complete_workflow(
    database: Database,
    context: WorkflowContext,
    state: dict[str, Any],
    results: WorkflowStageResults,
) -> QualityWorkflowSummary:
    review_reasons = [reason.to_dict() for reason in results.review_reasons]
    reused = list(results.reused_stages)
    base_state = (
        "semantic_ready"
        if results.semantic.status == "completed"
        else "semantic_ready_empty"
    )
    review_required = bool(review_reasons)
    state["workflow_state"] = (
        f"{base_state}_needs_review" if review_required else base_state
    )
    state["base_state"] = base_state
    state["review"] = {
        "required": review_required,
        "reasons": review_reasons,
    }
    state["detail"] = (
        "本地 V2 证据链已完成；存在需要人工检查的增强证据"
        if review_required
        else "本地 V2 证据链已完成；未调用云端 LLM"
    )
    state["reused_stages"] = reused
    state = effective_workflow_summary(database, state)
    effective_review = state.get("review") or {}
    final_review_reasons = list(effective_review.get("reasons") or [])
    database.finish_processing_run(
        context.workflow_run_id,
        status="completed",
        summary=state,
    )
    return QualityWorkflowSummary(
        workflow_run_id=context.workflow_run_id,
        session_id=context.session_id,
        recording_id=context.recording_id,
        state=str(state["workflow_state"]),
        asr_run_id=_required_run_id(results.asr),
        diarization_run_id=_required_run_id(results.diarization),
        v2d1_run_id=results.speech_recall.run_id,
        v2d2_run_id=results.identity_audit.run_id,
        semantic_run_id=results.semantic.run_id,
        review_required=bool(effective_review.get("required")),
        review_reasons=tuple(final_review_reasons),
        reused_stages=tuple(reused),
    )


def _validate_request(
    database: Database,
    recording_id: int | None,
    session_id: int,
    admission_mode: str,
) -> None:
    if admission_mode not in {"production", "shadow"}:
        raise ValueError("admission_mode 必须是 production 或 shadow")
    session = database.get_recording_session(session_id)
    if (
        recording_id is not None
        and session["legacy_recording_id"] is not None
        and int(session["legacy_recording_id"]) != recording_id
    ):
        raise ValueError("recording_id 与 session_id 不属于同一会话")


def _workflow_config(
    admission_mode: str,
    asr_settings: QualityAsrSettings,
    diarization_settings: QualityDiarizationSettings,
    semantic_settings: SemanticV2E02Settings,
) -> dict[str, Any]:
    return {
        "workflow_revision": "v2-session-quality-workflow-v1",
        "admission_mode": admission_mode,
        "asr": asr_settings.to_dict(),
        "diarization": diarization_settings.to_dict(),
        "enhancements": workflow_enhancement_config(),
        "semantic": asdict(semantic_settings),
        "cloud_provider_enabled": False,
    }


def _required_run_id(result: StageResult) -> int:
    if result.run_id is None:
        raise RuntimeError(f"{result.stage} 阶段完成但没有持久化 run ID")
    return result.run_id
