"""Review DTO validation and public candidate fields."""

from collections.abc import Mapping
from typing import Any


def _public_voice_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    # Never repair, widen or silently drop a stored window for audition.
    clips = [{key: raw.get(key) for key in ('media_id', 'start_ms', 'end_ms')}
             if isinstance(raw, dict) else {} for raw in candidate.get('representative_clips', [])]
    return {
        "prototype_id": str(candidate["prototype_id"]),
        "session_id": str(candidate["session_id"]),
        "speaker_track_id": str(candidate["speaker_track_id"]),
        "person_id": str(candidate["person_id"]),
        "person_name": str(candidate["person_name"]),
        "quality_score": _optional_number(candidate.get("quality_score")),
        "best_score": _optional_number(candidate.get("best_score")),
        "score_margin": _optional_number(candidate.get("score_margin")),
        "decision_tier": candidate.get("decision_tier"),
        "review_status": str(candidate.get("review_status") or "pending"),
        "representative_clips": clips,
    }


def _weakest_candidate_first(value: Mapping[str, Any]) -> tuple[float, float, str]:
    score = value.get("best_score")
    margin = value.get("score_margin")
    return (
        float(score) if isinstance(score, (int, float)) else -1.0,
        float(margin) if isinstance(margin, (int, float)) else -1.0,
        str(value["prototype_id"]),
    )


def _required_text(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_reason(payload: Mapping[str, Any]) -> str:
    value = payload.get("reason", "手机审核未采纳")
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise ValueError("review reason must be a non-empty string")
    return value.strip()


def _optional_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)
