from __future__ import annotations

import copy
import json
from typing import Any

from allday_asr.storage.database import Database

REVIEW_STATUSES = {"confirmed_speech", "rejected", "uncertain"}
IDENTITY_LABELS = {"self", "mother", "father", "tv", "other", "unknown"}
RESOLVED_WORKFLOW_REASON_CODES = {"possible_speech_high"}


def review_possible_speech_candidate(
    database: Database,
    run_id: int,
    *,
    candidate_id: str,
    status: str,
    note: str | None = None,
) -> dict[str, Any]:
    candidates = _possible_candidates(database, run_id)
    candidate = candidates.get(candidate_id)
    if candidate is None:
        raise ValueError("可能语音候选不属于当前 V2-D.1 run")
    if status not in REVIEW_STATUSES:
        raise ValueError("可能语音审核状态无效")
    row = database.upsert_v2d1_candidate_review(
        run_id,
        {
            "candidate_id": candidate_id,
            "session_start_ms": int(candidate["session_start_ms"]),
            "session_end_ms": int(candidate["session_end_ms"]),
            "status": status,
            "note": note,
        },
    )
    if status != "confirmed_speech":
        database.delete_v2d1_identity_label(run_id, candidate_id)
    return {
        "candidate_id": str(row["candidate_id"]),
        "status": str(row["status"]),
        "note": row["note"],
        "updated_at": str(row["updated_at"]),
        "review": v2d1_review_overview(database, run_id),
    }


def label_possible_speech_identity(
    database: Database,
    run_id: int,
    *,
    candidate_id: str,
    identity_label: str,
) -> dict[str, Any]:
    candidates = _possible_candidates(database, run_id)
    if candidate_id not in candidates:
        raise ValueError("可能语音候选不属于当前 V2-D.1 run")
    if identity_label not in IDENTITY_LABELS:
        raise ValueError("人物标签无效")
    row = database.upsert_v2d1_identity_label(
        run_id, candidate_id, identity_label
    )
    return {
        "candidate_id": str(row["candidate_id"]),
        "identity_label": str(row["identity_label"]),
        "updated_at": str(row["updated_at"]),
        "review": v2d1_review_overview(database, run_id),
    }


def complete_possible_speech_review(
    database: Database, run_id: int
) -> dict[str, Any]:
    candidates = _possible_candidates(database, run_id)
    reviews = {
        str(row["candidate_id"]): row
        for row in database.list_v2d1_candidate_reviews(run_id)
    }
    missing = sorted(set(candidates) - set(reviews))
    if missing:
        raise ValueError(f"还有 {len(missing)} 个可能语音候选尚未审核")
    database.upsert_v2d1_review_completion(
        run_id,
        candidate_count=len(candidates),
        reviewed_count=len(reviews),
    )
    return v2d1_review_overview(database, run_id)


def v2d1_review_overview(
    database: Database, run_id: int
) -> dict[str, Any]:
    candidates = _possible_candidates(database, run_id)
    review_rows = database.list_v2d1_candidate_reviews(run_id)
    reviews = {str(row["candidate_id"]): row for row in review_rows}
    identity_rows = database.list_v2d1_identity_labels(run_id)
    identities = {
        str(row["candidate_id"]): row
        for row in identity_rows
        if str(row["review_status"]) == "confirmed_speech"
    }
    completion = database.get_v2d1_review_completion(run_id)
    status_counts = {status: 0 for status in sorted(REVIEW_STATUSES)}
    for row in review_rows:
        status = str(row["status"])
        if status in status_counts:
            status_counts[status] += 1
    total = len(candidates)
    reviewed = len(set(candidates) & set(reviews))
    completed = bool(
        completion is not None
        and int(completion["candidate_count"]) == total
        and int(completion["reviewed_count"]) == reviewed
        and reviewed == total
    )
    confirmed_candidates = {
        candidate_id
        for candidate_id, row in reviews.items()
        if candidate_id in candidates and str(row["status"]) == "confirmed_speech"
    }
    labeled_candidates = confirmed_candidates & set(identities)
    identity_counts: dict[str, int] = {}
    for candidate_id in sorted(labeled_candidates):
        identity = str(identities[candidate_id]["identity_label"])
        identity_counts[identity] = identity_counts.get(identity, 0) + 1
    return {
        "run_id": run_id,
        "candidate_count": total,
        "reviewed_count": reviewed,
        "pending_count": max(0, total - reviewed),
        "status_counts": status_counts,
        "completed": completed,
        "completed_at": (
            str(completion["completed_at"]) if completed else None
        ),
        "confirmed_speech_count": len(confirmed_candidates),
        "identity_labeled_count": len(labeled_candidates),
        "identity_pending_count": len(confirmed_candidates - labeled_candidates),
        "identity_counts": identity_counts,
        "items": {
            candidate_id: {
                "status": str(row["status"]),
                "note": row["note"],
                "updated_at": str(row["updated_at"]),
                "identity_label": (
                    str(identities[candidate_id]["identity_label"])
                    if candidate_id in identities
                    else None
                ),
            }
            for candidate_id, row in reviews.items()
            if candidate_id in candidates
        },
    }


def effective_workflow_summary(
    database: Database, summary: dict[str, Any]
) -> dict[str, Any]:
    """Overlay completed human review without mutating the historical run."""
    result = copy.deepcopy(summary)
    enhancements = result.get("enhancements")
    if not isinstance(enhancements, dict):
        return result
    v2d1 = enhancements.get("v2d1")
    if not isinstance(v2d1, dict):
        return result
    run_id = int(v2d1.get("run_id") or 0)
    if not run_id:
        return result
    try:
        review = v2d1_review_overview(database, run_id)
    except (KeyError, ValueError):
        return result
    v2d1["human_review"] = {
        key: value for key, value in review.items() if key != "items"
    }
    if not review["completed"]:
        return result

    review_state = result.get("review")
    reasons = (
        list(review_state.get("reasons") or [])
        if isinstance(review_state, dict)
        else []
    )
    remaining = [
        reason
        for reason in reasons
        if str(reason.get("code") or "")
        not in RESOLVED_WORKFLOW_REASON_CODES
    ]
    base_state = str(result.get("base_state") or "")
    if not base_state:
        base_state = str(result.get("workflow_state") or "").removesuffix(
            "_needs_review"
        )
    required = bool(remaining)
    result["review"] = {
        "required": required,
        "reasons": remaining,
        "resolved_by_human_review": sorted(
            RESOLVED_WORKFLOW_REASON_CODES
            & {str(reason.get("code") or "") for reason in reasons}
        ),
    }
    result["workflow_state"] = (
        f"{base_state}_needs_review" if required else base_state
    )
    if not required:
        result["detail"] = "本地 V2 证据链已完成；D.1 人工检查已完成"
    return result


def _possible_candidates(database: Database, run_id: int) -> dict[str, Any]:
    run = database.get_processing_run(run_id)
    if str(run["run_kind"]) != "quality_diarization_v2d1":
        raise ValueError("可能语音审核只适用于 V2-D.1 run")
    if str(run["status"]) != "completed":
        raise ValueError("只能审核已完成的 V2-D.1 run")
    summary = _json_object(run["summary_json"])
    prediction_set_id = int(summary.get("rescue_prediction_set_id") or 0)
    if not prediction_set_id:
        return {}
    candidates: dict[str, Any] = {}
    for row in database.list_benchmark_predictions(
        prediction_set_id, prediction_kind="speech"
    ):
        metadata = _json_object(row["metadata_json"])
        if str(metadata.get("tier") or "") != "possible":
            continue
        candidates[f"possible-{int(row['id'])}"] = row
    return candidates


def _json_object(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    parsed = json.loads(str(value))
    return parsed if isinstance(parsed, dict) else {}

