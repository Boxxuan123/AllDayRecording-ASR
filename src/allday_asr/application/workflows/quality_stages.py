from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import asdict
from typing import Any

from allday_asr.application.diarization.identity_audit import (
    run_identity_contamination_audit,
)
from allday_asr.application.diarization.pipeline import (
    DiarizationSettings as QualityDiarizationSettings,
    run as run_quality_diarization,
)
from allday_asr.application.diarization.speech_recall import (
    V2D1Settings,
    run_quality_diarization_v2d1,
)
from allday_asr.application.semantic.pipeline import (
    SemanticSettings as SemanticV2E02Settings,
    run as run_semantic_v2e02,
)
from allday_asr.services.quality_asr import QualityAsrSettings, run_quality_asr
from allday_asr.services.session_readiness import evaluate_session_readiness
from allday_asr.storage.database import Database

from .quality_models import ReviewReason, StageResult, WorkflowContext

BackendFactory = Callable[[], Any]
ProgressReporter = Callable[[str, str], None]

D1_REVIEW_POSSIBLE_SHARE = 0.10
D1_REVIEW_POSSIBLE_MS = 60_000
D1_REVIEW_UNASSIGNED_SHARE = 0.02
D1_REVIEW_UNASSIGNED_TOKENS = 20
D2_REVIEW_MIN_COVERAGE = 0.80


def workflow_enhancement_config() -> dict[str, Any]:
    return {
        "v2d1": V2D1Settings().to_dict(),
        "v2d2_when_frozen_identity_truth_exists": True,
        "v2d3_requires_human_target_and_review": True,
        "review_thresholds": {
            "possible_speech_share": D1_REVIEW_POSSIBLE_SHARE,
            "possible_speech_ms": D1_REVIEW_POSSIBLE_MS,
            "unassigned_token_share": D1_REVIEW_UNASSIGNED_SHARE,
            "unassigned_tokens": D1_REVIEW_UNASSIGNED_TOKENS,
            "identity_coverage": D2_REVIEW_MIN_COVERAGE,
        },
    }


def verify_admission(
    database: Database,
    context: WorkflowContext,
    report: ProgressReporter,
) -> StageResult:
    report(
        "admission_verifying",
        f"检查 {context.admission_mode} 会话准入条件",
    )
    readiness = evaluate_session_readiness(
        database,
        context.session_id,
        verify_backups=context.admission_mode == "production",
    )
    admitted = (
        bool(readiness["production_ready"])
        if context.admission_mode == "production"
        else bool(readiness["shadow_ready"])
    )
    if not admitted:
        reasons = readiness["blocking_reasons"] + (
            readiness["production_blockers"]
            if context.admission_mode == "production"
            else []
        )
        raise RuntimeError(
            f"会话未达到 {context.admission_mode} 准入条件："
            + "；".join(reasons)
        )

    report("integrity_verifying", "重新校验清单和每个原始音频实例")
    integrity = readiness["integrity"]
    failures = [
        item for item in integrity["instances"] if item["status"] != "verified"
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
    return StageResult(
        stage="admission",
        run_id=None,
        status="completed",
        reused=False,
        summary={
            "readiness": {
                "state": readiness["state"],
                "production_backup_id": readiness["production_backup_id"],
            },
            "integrity": integrity,
        },
    )


def resolve_or_run_asr(
    database: Database,
    context: WorkflowContext,
    *,
    settings: QualityAsrSettings,
    primary_factory: BackendFactory,
    secondary_factory: BackendFactory,
    report: ProgressReporter,
) -> StageResult:
    report("asr_running", "检查或运行 V2-C 双模型 ASR")
    matching_run = _matching_asr_run(database, context, settings)
    reused = matching_run is not None and str(matching_run["status"]) == "completed"
    if reused:
        run_id = int(matching_run["id"])
    else:
        resume_run_id = int(matching_run["id"]) if matching_run is not None else None
        result = run_quality_asr(
            database,
            context.recording_id,
            session_id=context.session_id,
            settings=settings,
            primary_factory=primary_factory,
            secondary_factory=secondary_factory,
            resume_run_id=resume_run_id,
        )
        run_id = result.run_id
    return StageResult(
        stage="asr",
        run_id=run_id,
        status="completed",
        reused=reused,
        summary={
            "run_id": run_id,
            "status": "completed",
            "reused": reused,
        },
    )


def resolve_or_run_diarization(
    database: Database,
    context: WorkflowContext,
    *,
    asr_run_id: int,
    settings: QualityDiarizationSettings,
    backend_factory: BackendFactory,
    report: ProgressReporter,
) -> StageResult:
    report("diarization_running", "检查或运行 V2-D 重叠说话人时间轴")
    matching_run = _matching_diarization_run(
        database,
        context.session_id,
        asr_run_id=asr_run_id,
        settings=settings,
    )
    reused = matching_run is not None
    if reused:
        run_id = int(matching_run["id"])
    else:
        result = run_quality_diarization(
            database,
            context.recording_id,
            session_id=context.session_id,
            asr_run_id=asr_run_id,
            settings=settings,
            backend_factory=backend_factory,
        )
        run_id = result.run_id
    return StageResult(
        stage="diarization",
        run_id=run_id,
        status="completed",
        reused=reused,
        summary={
            "run_id": run_id,
            "status": "completed",
            "reused": reused,
        },
    )


def run_optional_speech_recall(
    database: Database,
    context: WorkflowContext,
    *,
    asr_run_id: int,
    diarization_run_id: int,
    report: ProgressReporter,
) -> StageResult:
    report(
        "enhancement_v2d1_running",
        "检查或生成 V2-D.1 确定/可能语音双层证据",
    )
    settings = V2D1Settings()
    try:
        matching_run = _matching_v2d1_run(
            database,
            context.session_id,
            diarization_run_id=diarization_run_id,
            asr_run_id=asr_run_id,
            settings=settings,
        )
        reused = matching_run is not None
        if reused:
            run_id = int(matching_run["id"])
            stage_summary = _json_object(matching_run["summary_json"])
        else:
            result = run_quality_diarization_v2d1(
                database,
                context.recording_id,
                session_id=context.session_id,
                diarization_run_id=diarization_run_id,
                settings=settings,
            )
            run_id = result.run_id
            stage_summary = {
                "detected_regions": result.detected_regions,
                "possible_regions": result.possible_regions,
                "detected_ms": result.detected_ms,
                "possible_ms": result.possible_ms,
            }
        review_reasons = _v2d1_review_reasons(
            stage_summary,
            _processing_run_summary(database, diarization_run_id),
        )
        return StageResult(
            stage="v2d1",
            run_id=run_id,
            status="completed",
            reused=reused,
            summary={
                "status": "completed",
                "run_id": run_id,
                "reused": reused,
                "detected_regions": int(
                    stage_summary.get("detected_regions") or 0
                ),
                "possible_regions": int(
                    stage_summary.get("possible_regions") or 0
                ),
                "detected_ms": int(stage_summary.get("detected_ms") or 0),
                "possible_ms": int(stage_summary.get("possible_ms") or 0),
            },
            review_reasons=review_reasons,
        )
    # Enhancement failures are reviewable and must not suppress the base result.
    except Exception as exc:  # noqa: BLE001
        reason = ReviewReason(
            code="v2d1_failed",
            stage="v2d1",
            message="V2-D.1 自动增强失败，需要检查运行记录。",
        )
        return StageResult(
            stage="v2d1",
            run_id=None,
            status="failed",
            reused=False,
            summary={"status": "failed", "error": repr(exc)},
            review_reasons=(reason,),
        )


def run_optional_identity_audit(
    database: Database,
    context: WorkflowContext,
    *,
    diarization_run_id: int,
    report: ProgressReporter,
) -> StageResult:
    identity_truth = _latest_frozen_speaker_truth_set(
        database, context.session_id
    )
    if identity_truth is None:
        return StageResult(
            stage="v2d2",
            run_id=None,
            status="not_applicable",
            reused=False,
            summary={
                "status": "not_applicable",
                "reason": "no_frozen_speaker_truth",
                "detail": "当前会话没有冻结的人工身份真值，已跳过污染审计。",
            },
        )

    truth_set_id = int(identity_truth["id"])
    report(
        "enhancement_v2d2_running",
        f"使用冻结身份真值 #{truth_set_id} 检查匿名簇污染",
    )
    try:
        matching_run = _matching_v2d2_run(
            database,
            context.session_id,
            diarization_run_id=diarization_run_id,
            truth_set_id=truth_set_id,
        )
        reused = matching_run is not None
        if reused:
            run_id = int(matching_run["id"])
            stage_summary = _json_object(matching_run["summary_json"])
        else:
            result = run_identity_contamination_audit(
                database,
                context.recording_id,
                session_id=context.session_id,
                diarization_run_id=diarization_run_id,
                truth_set_id=truth_set_id,
            )
            run_id = result.run_id
            stage_summary = {
                "coverage": result.coverage,
                "human_speakers": result.human_speakers,
                "contaminated_speakers": result.contaminated_speakers,
            }
        review_reasons = _v2d2_review_reasons(stage_summary)
        status = "needs_review" if review_reasons else "completed"
        return StageResult(
            stage="v2d2",
            run_id=run_id,
            status=status,
            reused=reused,
            summary={
                "status": status,
                "run_id": run_id,
                "truth_set_id": truth_set_id,
                "reused": reused,
                "coverage": float(stage_summary.get("coverage") or 0.0),
                "contaminated_speakers": list(
                    stage_summary.get("contaminated_speakers") or []
                ),
            },
            review_reasons=review_reasons,
        )
    # Sparse truth can be incomplete; preserve C/D/E and surface the audit.
    except Exception as exc:  # noqa: BLE001
        reason = ReviewReason(
            code="v2d2_failed",
            stage="v2d2",
            message="V2-D.2 身份污染审计失败，需要检查真值或运行记录。",
        )
        return StageResult(
            stage="v2d2",
            run_id=None,
            status="failed",
            reused=False,
            summary={
                "status": "failed",
                "truth_set_id": truth_set_id,
                "error": repr(exc),
            },
            review_reasons=(reason,),
        )


def inspect_identity_mining_state(
    database: Database,
    context: WorkflowContext,
    *,
    diarization_run_id: int,
    identity_audit: StageResult,
) -> StageResult:
    latest_run = _latest_v2d3_run(
        database, context.session_id, diarization_run_id
    )
    if latest_run is not None:
        run_id = int(latest_run["id"])
        stage_summary = _json_object(latest_run["summary_json"])
        reviews = database.list_identity_candidate_reviews(run_id)
        selected = int(stage_summary.get("selected_candidates") or 0)
        pending = max(0, selected - len(reviews))
        review_reasons: tuple[ReviewReason, ...] = ()
        if pending:
            review_reasons = (
                ReviewReason(
                    code="v2d3_candidates_pending",
                    stage="v2d3",
                    message=f"有 {pending} 个身份扩样候选等待人工确认。",
                    details={"pending_candidates": pending},
                ),
            )
        status = "needs_review" if pending else "completed"
        return StageResult(
            stage="v2d3",
            run_id=run_id,
            status=status,
            reused=False,
            summary={
                "status": status,
                "run_id": run_id,
                "target_identity": stage_summary.get("target_identity"),
                "selected_candidates": selected,
                "reviewed_candidates": len(reviews),
                "pending_candidates": pending,
            },
            review_reasons=review_reasons,
        )

    audit_requires_target = any(
        reason.code
        in {
            "identity_cluster_contamination",
            "identity_truth_coverage_low",
            "identity_fragmented",
        }
        for reason in identity_audit.review_reasons
    )
    if audit_requires_target:
        reason = ReviewReason(
            code="v2d3_target_identity_required",
            stage="v2d3",
            message="需要人工选择要扩充的目标身份。",
        )
        return StageResult(
            stage="v2d3",
            run_id=None,
            status="needs_review",
            reused=False,
            summary={
                "status": "needs_review",
                "reason": "target_identity_required",
                "detail": "身份审计发现问题；需要人工选择目标身份后生成扩样候选。",
            },
            review_reasons=(reason,),
        )

    reason = (
        "no_frozen_speaker_truth"
        if identity_audit.status == "not_applicable"
        else "identity_audit_clean"
    )
    return StageResult(
        stage="v2d3",
        run_id=None,
        status="not_applicable",
        reused=False,
        summary={
            "status": "not_applicable",
            "reason": reason,
            "detail": "当前没有需要生成的身份扩样候选。",
        },
    )


def resolve_or_run_semantic(
    database: Database,
    context: WorkflowContext,
    *,
    asr_run_id: int,
    diarization_run_id: int,
    committed_tokens: Sequence[Any],
    settings: SemanticV2E02Settings,
    report: ProgressReporter,
) -> StageResult:
    if not committed_tokens:
        return StageResult(
            stage="semantic",
            run_id=None,
            status="skipped",
            reused=False,
            summary={
                "run_id": None,
                "status": "skipped",
                "reason": "no_committed_asr_tokens",
            },
        )

    report("semantic_running", "检查或生成 V2-E.0.2 本地语义证据")
    matching_run = _matching_semantic_run(
        database,
        context.session_id,
        asr_run_id=asr_run_id,
        diarization_run_id=diarization_run_id,
        settings=settings,
    )
    reused = matching_run is not None
    if reused:
        run_id = int(matching_run["id"])
    else:
        result = run_semantic_v2e02(
            database,
            context.recording_id,
            session_id=context.session_id,
            asr_run_id=asr_run_id,
            diarization_run_id=diarization_run_id,
            settings=settings,
        )
        run_id = result.run_id
    return StageResult(
        stage="semantic",
        run_id=run_id,
        status="completed",
        reused=reused,
        summary={
            "run_id": run_id,
            "status": "completed",
            "reused": reused,
        },
    )


def _processing_run_summary(database: Database, run_id: int) -> dict[str, Any]:
    try:
        return _json_object(database.get_processing_run(run_id)["summary_json"])
    except KeyError:
        return {}


def _v2d1_review_reasons(
    v2d1_summary: dict[str, Any], diarization_summary: dict[str, Any]
) -> tuple[ReviewReason, ...]:
    reasons: list[ReviewReason] = []
    detected_ms = int(v2d1_summary.get("detected_ms") or 0)
    possible_ms = int(v2d1_summary.get("possible_ms") or 0)
    expanded_ms = detected_ms + possible_ms
    possible_share = possible_ms / expanded_ms if expanded_ms else 0.0
    if (
        possible_ms >= D1_REVIEW_POSSIBLE_MS
        or possible_share >= D1_REVIEW_POSSIBLE_SHARE
    ):
        reasons.append(
            ReviewReason(
                code="possible_speech_high",
                stage="v2d1",
                message="可能漏检语音较多，建议优先试听 D.1 召回队列。",
                details={
                    "possible_ms": possible_ms,
                    "possible_share": possible_share,
                },
            )
        )
    attributed_tokens = int(diarization_summary.get("attributed_tokens") or 0)
    unassigned_tokens = int(diarization_summary.get("unassigned_tokens") or 0)
    unassigned_share = (
        unassigned_tokens / attributed_tokens if attributed_tokens else 0.0
    )
    if (
        unassigned_tokens >= D1_REVIEW_UNASSIGNED_TOKENS
        or (
            unassigned_tokens > 0
            and unassigned_share >= D1_REVIEW_UNASSIGNED_SHARE
        )
    ):
        reasons.append(
            ReviewReason(
                code="unassigned_tokens_high",
                stage="v2d1",
                message="无归属 ASR token 较多，需要检查说话人漏检或错配。",
                details={
                    "unassigned_tokens": unassigned_tokens,
                    "unassigned_share": unassigned_share,
                },
            )
        )
    return tuple(reasons)


def _v2d2_review_reasons(
    summary: dict[str, Any],
) -> tuple[ReviewReason, ...]:
    reasons: list[ReviewReason] = []
    contaminated = list(summary.get("contaminated_speakers") or [])
    if contaminated:
        reasons.append(
            ReviewReason(
                code="identity_cluster_contamination",
                stage="v2d2",
                message="匿名 speaker 簇混入了多个已标注人物。",
                details={"speakers": contaminated},
            )
        )
    coverage = float(summary.get("coverage") or 0.0)
    if coverage < D2_REVIEW_MIN_COVERAGE:
        reasons.append(
            ReviewReason(
                code="identity_truth_coverage_low",
                stage="v2d2",
                message="人工身份真值的说话人覆盖率低于 80%。",
                details={"coverage": coverage},
            )
        )
    fragmented = [
        str(item.get("identity") or "")
        for item in summary.get("human_speakers") or []
        if bool(item.get("fragmented"))
    ]
    fragmented = [value for value in fragmented if value]
    if fragmented:
        reasons.append(
            ReviewReason(
                code="identity_fragmented",
                stage="v2d2",
                message="同一人工身份被拆分到了多个匿名 speaker。",
                details={"identities": fragmented},
            )
        )
    return tuple(reasons)


def _latest_frozen_speaker_truth_set(
    database: Database, session_id: int
) -> Any | None:
    for truth_set in reversed(database.list_truth_sets(session_id)):
        if str(truth_set["status"]) != "frozen":
            continue
        annotations = database.list_truth_annotations(
            int(truth_set["id"]), annotation_kind="speaker"
        )
        if any(str(row["label"] or "").strip() for row in annotations):
            return truth_set
    return None


def _matching_v2d1_run(
    database: Database,
    session_id: int,
    *,
    diarization_run_id: int,
    asr_run_id: int,
    settings: V2D1Settings,
) -> Any | None:
    expected = settings.to_dict()
    for row in reversed(database.list_session_processing_runs(session_id)):
        if (
            str(row["run_kind"]) != "quality_diarization_v2d1"
            or str(row["status"]) != "completed"
        ):
            continue
        config = _json_object(row["config_json"])
        if (
            int(config.get("diarization_run_id") or 0) == diarization_run_id
            and int(config.get("asr_run_id") or 0) == asr_run_id
            and all(config.get(key) == value for key, value in expected.items())
        ):
            return row
    return None


def _matching_v2d2_run(
    database: Database,
    session_id: int,
    *,
    diarization_run_id: int,
    truth_set_id: int,
) -> Any | None:
    for row in reversed(database.list_session_processing_runs(session_id)):
        if (
            str(row["run_kind"]) != "quality_diarization_v2d2"
            or str(row["status"]) != "completed"
        ):
            continue
        summary = _json_object(row["summary_json"])
        if (
            int(summary.get("diarization_run_id") or 0) == diarization_run_id
            and int(summary.get("truth_set_id") or 0) == truth_set_id
        ):
            return row
    return None


def _latest_v2d3_run(
    database: Database, session_id: int, diarization_run_id: int
) -> Any | None:
    for row in reversed(database.list_session_processing_runs(session_id)):
        if (
            str(row["run_kind"]) != "quality_diarization_v2d3"
            or str(row["status"]) != "completed"
        ):
            continue
        summary = _json_object(row["summary_json"])
        if int(summary.get("diarization_run_id") or 0) == diarization_run_id:
            return row
    return None


def _matching_asr_run(
    database: Database,
    context: WorkflowContext,
    settings: QualityAsrSettings,
) -> Any | None:
    if not settings.model_signature:
        return None
    matches = [
        row
        for row in database.list_session_processing_runs(context.session_id)
        if str(row["run_kind"]) == "quality_asr_v2c"
        and str(row["config_sha256"]) == settings.sha256()
        and str(row["input_fingerprint"]) == context.input_fingerprint
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
            and int(summary.get("diarization_run_id") or 0)
            == diarization_run_id
            and summary.get("provider") == "local_mock"
            and all(
                config.get(key) == value
                for key, value in asdict(settings).items()
            )
        ):
            return row
    return None


def _json_object(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(str(value))
    return parsed if isinstance(parsed, dict) else {}
