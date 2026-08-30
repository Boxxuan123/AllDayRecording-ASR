from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from allday_asr.domain.hashing import canonical_json_sha256 as _sha256_mapping
from allday_asr.services.quality_asr import QualityAsrSettings, run_quality_asr
from allday_asr.services.quality_diarization import (
    QualityDiarizationSettings,
    run_quality_diarization,
)
from allday_asr.services.quality_diarization_v2d1 import (
    V2D1Settings,
    run_quality_diarization_v2d1,
)
from allday_asr.services.quality_diarization_v2d1_review import (
    effective_workflow_summary,
)
from allday_asr.services.quality_diarization_v2d2 import (
    run_identity_contamination_audit,
)
from allday_asr.services.semantic_v2e02 import (
    SemanticV2E02Settings,
    run_semantic_v2e02,
)
from allday_asr.services.session_readiness import evaluate_session_readiness
from allday_asr.storage.database import Database

ProgressCallback = Callable[[str, str], None]
BackendFactory = Callable[[], Any]

D1_REVIEW_POSSIBLE_SHARE = 0.10
D1_REVIEW_POSSIBLE_MS = 60_000
D1_REVIEW_UNASSIGNED_SHARE = 0.02
D1_REVIEW_UNASSIGNED_TOKENS = 20
D2_REVIEW_MIN_COVERAGE = 0.80


@dataclass(frozen=True)
class QualityWorkflowSummary:
    workflow_run_id: int
    session_id: int
    recording_id: int | None
    state: str
    asr_run_id: int
    diarization_run_id: int
    v2d1_run_id: int | None
    v2d2_run_id: int | None
    semantic_run_id: int | None
    review_required: bool
    review_reasons: tuple[dict[str, Any], ...]
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
        "enhancements": {
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
        },
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
        diarization_summary = _processing_run_summary(
            database, diarization_run_id
        )
        enhancement_state: dict[str, Any] = {}
        state["enhancements"] = enhancement_state
        review_reasons: list[dict[str, Any]] = []
        v2d1_run_id: int | None = None
        v2d2_run_id: int | None = None

        report(
            "enhancement_v2d1_running",
            "检查或生成 V2-D.1 确定/可能语音双层证据",
        )
        v2d1_settings = V2D1Settings()
        try:
            v2d1_run = _matching_v2d1_run(
                database,
                session_id,
                diarization_run_id=diarization_run_id,
                asr_run_id=asr_run_id,
                settings=v2d1_settings,
            )
            if v2d1_run is not None:
                v2d1_run_id = int(v2d1_run["id"])
                v2d1_summary = _json_object(v2d1_run["summary_json"])
                reused.append("v2d1")
            else:
                v2d1_result = run_quality_diarization_v2d1(
                    database,
                    recording_id,
                    session_id=session_id,
                    diarization_run_id=diarization_run_id,
                    settings=v2d1_settings,
                )
                v2d1_run_id = v2d1_result.run_id
                v2d1_summary = {
                    "detected_regions": v2d1_result.detected_regions,
                    "possible_regions": v2d1_result.possible_regions,
                    "detected_ms": v2d1_result.detected_ms,
                    "possible_ms": v2d1_result.possible_ms,
                }
            enhancement_state["v2d1"] = {
                "status": "completed",
                "run_id": v2d1_run_id,
                "reused": "v2d1" in reused,
                "detected_regions": int(
                    v2d1_summary.get("detected_regions") or 0
                ),
                "possible_regions": int(
                    v2d1_summary.get("possible_regions") or 0
                ),
                "detected_ms": int(v2d1_summary.get("detected_ms") or 0),
                "possible_ms": int(v2d1_summary.get("possible_ms") or 0),
            }
            review_reasons.extend(
                _v2d1_review_reasons(v2d1_summary, diarization_summary)
            )
        # Enhancement failures are reviewable and must not suppress the base result.
        except Exception as exc:  # noqa: BLE001
            enhancement_state["v2d1"] = {
                "status": "failed",
                "error": repr(exc),
            }
            review_reasons.append(
                {
                    "code": "v2d1_failed",
                    "stage": "v2d1",
                    "message": "V2-D.1 自动增强失败，需要检查运行记录。",
                }
            )

        identity_truth = _latest_frozen_speaker_truth_set(database, session_id)
        d2_review_reasons: list[dict[str, Any]] = []
        if identity_truth is None:
            enhancement_state["v2d2"] = {
                "status": "not_applicable",
                "reason": "no_frozen_speaker_truth",
                "detail": "当前会话没有冻结的人工身份真值，已跳过污染审计。",
            }
        else:
            truth_set_id = int(identity_truth["id"])
            report(
                "enhancement_v2d2_running",
                f"使用冻结身份真值 #{truth_set_id} 检查匿名簇污染",
            )
            try:
                v2d2_run = _matching_v2d2_run(
                    database,
                    session_id,
                    diarization_run_id=diarization_run_id,
                    truth_set_id=truth_set_id,
                )
                if v2d2_run is not None:
                    v2d2_run_id = int(v2d2_run["id"])
                    v2d2_summary = _json_object(v2d2_run["summary_json"])
                    reused.append("v2d2")
                else:
                    v2d2_result = run_identity_contamination_audit(
                        database,
                        recording_id,
                        session_id=session_id,
                        diarization_run_id=diarization_run_id,
                        truth_set_id=truth_set_id,
                    )
                    v2d2_run_id = v2d2_result.run_id
                    v2d2_summary = {
                        "coverage": v2d2_result.coverage,
                        "human_speakers": v2d2_result.human_speakers,
                        "contaminated_speakers": (
                            v2d2_result.contaminated_speakers
                        ),
                    }
                d2_review_reasons = _v2d2_review_reasons(v2d2_summary)
                review_reasons.extend(d2_review_reasons)
                enhancement_state["v2d2"] = {
                    "status": (
                        "needs_review" if d2_review_reasons else "completed"
                    ),
                    "run_id": v2d2_run_id,
                    "truth_set_id": truth_set_id,
                    "reused": "v2d2" in reused,
                    "coverage": float(v2d2_summary.get("coverage") or 0.0),
                    "contaminated_speakers": list(
                        v2d2_summary.get("contaminated_speakers") or []
                    ),
                }
            # Sparse truth can be incomplete; preserve C/D/E and surface the audit.
            except Exception as exc:  # noqa: BLE001
                enhancement_state["v2d2"] = {
                    "status": "failed",
                    "truth_set_id": truth_set_id,
                    "error": repr(exc),
                }
                review_reasons.append(
                    {
                        "code": "v2d2_failed",
                        "stage": "v2d2",
                        "message": "V2-D.2 身份污染审计失败，需要检查真值或运行记录。",
                    }
                )

        v2d3_run = _latest_v2d3_run(database, session_id, diarization_run_id)
        if v2d3_run is not None:
            v2d3_summary = _json_object(v2d3_run["summary_json"])
            reviews = database.list_identity_candidate_reviews(
                int(v2d3_run["id"])
            )
            selected = int(v2d3_summary.get("selected_candidates") or 0)
            pending = max(0, selected - len(reviews))
            enhancement_state["v2d3"] = {
                "status": "needs_review" if pending else "completed",
                "run_id": int(v2d3_run["id"]),
                "target_identity": v2d3_summary.get("target_identity"),
                "selected_candidates": selected,
                "reviewed_candidates": len(reviews),
                "pending_candidates": pending,
            }
            if pending:
                review_reasons.append(
                    {
                        "code": "v2d3_candidates_pending",
                        "stage": "v2d3",
                        "message": f"有 {pending} 个身份扩样候选等待人工确认。",
                        "pending_candidates": pending,
                    }
                )
        elif d2_review_reasons:
            enhancement_state["v2d3"] = {
                "status": "needs_review",
                "reason": "target_identity_required",
                "detail": "身份审计发现问题；需要人工选择目标身份后生成扩样候选。",
            }
            review_reasons.append(
                {
                    "code": "v2d3_target_identity_required",
                    "stage": "v2d3",
                    "message": "需要人工选择要扩充的目标身份。",
                }
            )
        else:
            enhancement_state["v2d3"] = {
                "status": "not_applicable",
                "reason": (
                    "no_frozen_speaker_truth"
                    if identity_truth is None
                    else "identity_audit_clean"
                ),
                "detail": "当前没有需要生成的身份扩样候选。",
            }

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
            base_state = "semantic_ready"
        else:
            state["stages"]["semantic"] = {
                "run_id": None,
                "status": "skipped",
                "reason": "no_committed_asr_tokens",
            }
            base_state = "semantic_ready_empty"

        review_required = bool(review_reasons)
        final_state = (
            f"{base_state}_needs_review" if review_required else base_state
        )
        state["workflow_state"] = final_state
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
        final_state = str(state["workflow_state"])
        effective_review = state.get("review") or {}
        review_required = bool(effective_review.get("required"))
        review_reasons = list(effective_review.get("reasons") or [])
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
            v2d1_run_id=v2d1_run_id,
            v2d2_run_id=v2d2_run_id,
            semantic_run_id=semantic_run_id,
            review_required=review_required,
            review_reasons=tuple(review_reasons),
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


def _processing_run_summary(database: Database, run_id: int) -> dict[str, Any]:
    try:
        return _json_object(database.get_processing_run(run_id)["summary_json"])
    except KeyError:
        return {}


def _v2d1_review_reasons(
    v2d1_summary: dict[str, Any], diarization_summary: dict[str, Any]
) -> list[dict[str, Any]]:
    reasons: list[dict[str, Any]] = []
    detected_ms = int(v2d1_summary.get("detected_ms") or 0)
    possible_ms = int(v2d1_summary.get("possible_ms") or 0)
    expanded_ms = detected_ms + possible_ms
    possible_share = possible_ms / expanded_ms if expanded_ms else 0.0
    if (
        possible_ms >= D1_REVIEW_POSSIBLE_MS
        or possible_share >= D1_REVIEW_POSSIBLE_SHARE
    ):
        reasons.append(
            {
                "code": "possible_speech_high",
                "stage": "v2d1",
                "message": "可能漏检语音较多，建议优先试听 D.1 召回队列。",
                "possible_ms": possible_ms,
                "possible_share": possible_share,
            }
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
            {
                "code": "unassigned_tokens_high",
                "stage": "v2d1",
                "message": "无归属 ASR token 较多，需要检查说话人漏检或错配。",
                "unassigned_tokens": unassigned_tokens,
                "unassigned_share": unassigned_share,
            }
        )
    return reasons


def _v2d2_review_reasons(summary: dict[str, Any]) -> list[dict[str, Any]]:
    reasons: list[dict[str, Any]] = []
    contaminated = list(summary.get("contaminated_speakers") or [])
    if contaminated:
        reasons.append(
            {
                "code": "identity_cluster_contamination",
                "stage": "v2d2",
                "message": "匿名 speaker 簇混入了多个已标注人物。",
                "speakers": contaminated,
            }
        )
    coverage = float(summary.get("coverage") or 0.0)
    if coverage < D2_REVIEW_MIN_COVERAGE:
        reasons.append(
            {
                "code": "identity_truth_coverage_low",
                "stage": "v2d2",
                "message": "人工身份真值的说话人覆盖率低于 80%。",
                "coverage": coverage,
            }
        )
    fragmented = [
        str(item.get("identity") or "")
        for item in summary.get("human_speakers") or []
        if bool(item.get("fragmented"))
    ]
    fragmented = [value for value in fragmented if value]
    if fragmented:
        reasons.append(
            {
                "code": "identity_fragmented",
                "stage": "v2d2",
                "message": "同一人工身份被拆分到了多个匿名 speaker。",
                "identities": fragmented,
            }
        )
    return reasons


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
