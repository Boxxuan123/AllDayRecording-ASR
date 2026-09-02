from __future__ import annotations

import base64
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from allday_asr.v3.adapters.audio.tools import extract_clip
from allday_asr.v3.interfaces.transfer.devices import DeviceConflictError


MAX_REVIEW_AUDIO_BYTES = 16 * 1024 * 1024
MAX_REVIEW_AUDIO_DURATION_MS = 15_000
REVIEW_AUDIO_LOUDNESS_FILTER = "loudnorm=I=-20:LRA=7:TP=-3"


class DeviceReviewService:
    """Expose the authoritative review inbox to a paired phone.

    Reviews are snapshots rather than change-log projections because resolving an
    item on either screen must remove it from both screens on the next refresh.
    Every mutation is checked against a freshly generated snapshot.
    """

    def __init__(self, core: Any) -> None:
        self.core = core

    def snapshot(self) -> dict[str, Any]:
        candidates = self.core.people.list_review_candidates(None, "pending", 500)
        by_prototype = {
            str(candidate["prototype_id"]): candidate for candidate in candidates
        }
        items: list[dict[str, Any]] = []
        for raw in self.core.desktop.list_reviews(500):
            item = dict(raw)
            context = dict(item.get("context") or {})
            if (
                item.get("kind") == "voice_identity"
                and context.get("voice_mode") == "known_person"
            ):
                enriched = []
                for prototype_id in context.get("prototype_ids", []):
                    candidate = by_prototype.get(str(prototype_id))
                    if candidate is not None:
                        enriched.append(_public_voice_candidate(candidate))
                enriched.sort(key=_weakest_candidate_first)
                context["voice_candidates"] = enriched
            item["context"] = context
            items.append(item)
        return {"items": items}

    def resolve(
        self, device_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        review_id = _required_text(payload, "review_id")
        action = _required_text(payload, "action")
        item = self._current_item(review_id)
        actor = f"phone-device:{device_id}"
        kind = str(item["kind"])
        source_id = str(item["source_id"])
        context = dict(item.get("context") or {})

        if kind == "voice_identity":
            if context.get("voice_mode") == "speaker_discovery":
                if action == "create_person":
                    result = self.core.people.create_and_label(
                        source_id, _required_text(payload, "display_name"), actor
                    )
                elif action == "ignore":
                    result = self.core.people.ignore(
                        source_id, _optional_reason(payload), actor
                    )
                else:
                    raise ValueError("unsupported speaker discovery review action")
            else:
                decision = {
                    "confirm": "confirmed",
                    "reject": "rejected",
                    "uncertain": "uncertain",
                }.get(action)
                if decision is None:
                    raise ValueError("unsupported voice review action")
                prototype_id = _required_text(payload, "prototype_id")
                allowed = {str(value) for value in context.get("prototype_ids", [])}
                if prototype_id not in allowed:
                    raise DeviceConflictError(
                        "voice sample no longer belongs to this review"
                    )
                result = self.core.people.review_prototype(
                    prototype_id,
                    str(item["person_id"]),
                    decision,
                    note="手机审核",
                    actor=actor,
                )
        elif kind == "reminder":
            if action == "confirm":
                result = self.core.reminders.confirm(source_id, actor)
            elif action == "ignore":
                result = self.core.reminders.ignore(
                    source_id, actor, _optional_reason(payload)
                )
            else:
                raise ValueError("unsupported reminder review action")
        elif kind == "person_memory":
            if action == "confirm":
                result = self.core.person_memory.confirm(source_id, actor=actor)
            elif action == "reject":
                result = self.core.person_memory.retract(source_id, actor=actor)
            else:
                raise ValueError("unsupported person memory review action")
        elif kind == "knowledge_proposal":
            if action == "confirm":
                result = self.core.knowledge.accept_proposal(
                    source_id, actor
                ).as_dict()
            elif action == "reject":
                result = self.core.knowledge.reject_proposal(
                    source_id, actor, _optional_reason(payload)
                ).as_dict()
            else:
                raise ValueError("unsupported knowledge proposal review action")
        else:
            raise ValueError("this review kind cannot be resolved from the phone")
        return {"result": result, "reviews": self.snapshot()}

    def audio(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        review_id = _required_text(payload, "review_id")
        prototype_id = _required_text(payload, "prototype_id")
        item = self._current_item(review_id)
        context = dict(item.get("context") or {})
        if (
            item.get("kind") != "voice_identity"
            or context.get("voice_mode") != "known_person"
            or prototype_id
            not in {str(value) for value in context.get("prototype_ids", [])}
        ):
            raise DeviceConflictError("voice sample no longer belongs to this review")
        candidates = self.core.people.list_review_candidates(None, "pending", 500)
        candidate = next(
            (
                value
                for value in candidates
                if str(value["prototype_id"]) == prototype_id
            ),
            None,
        )
        if candidate is None:
            raise DeviceConflictError("voice sample is no longer pending")
        clips = candidate.get("representative_clips") or []
        if not clips or not isinstance(clips[0], dict):
            raise KeyError("voice sample has no playable evidence")
        clip = clips[0]
        media_id = _clip_text(clip, "media_id")
        start_ms = _clip_integer(clip.get("start_ms"), 0)
        end_ms = _clip_integer(clip.get("end_ms"), start_ms + 6_000)
        if end_ms <= start_ms:
            end_ms = start_ms + 6_000
        descriptor = self.core.desktop.media(media_id)
        clip_end_ms = min(end_ms, start_ms + MAX_REVIEW_AUDIO_DURATION_MS)
        source_path = self.core.audio_store.path_for(str(descriptor["storage_key"]))
        temp_root = Path(self.core.audio_store.root).parent / "review-audio-temp"
        data = _normalized_review_audio(
            source_path, start_ms, clip_end_ms, temp_root=temp_root
        )
        if not data or len(data) > MAX_REVIEW_AUDIO_BYTES:
            raise ValueError("normalized voice review audio is too large for mobile playback")
        return {
            "review_id": review_id,
            "prototype_id": prototype_id,
            "format": "wav",
            "data_base64url": base64.urlsafe_b64encode(data)
            .rstrip(b"=")
            .decode("ascii"),
            "start_ms": 0,
            "end_ms": clip_end_ms - start_ms,
        }

    def _current_item(self, review_id: str) -> dict[str, Any]:
        for item in self.snapshot()["items"]:
            if item.get("review_id") == review_id:
                return item
        raise DeviceConflictError("review item is no longer pending")


def _normalized_review_audio(
    source: Path, start_ms: int, end_ms: int, *, temp_root: Path
) -> bytes:
    temp_root.mkdir(parents=True, exist_ok=True)
    destination = temp_root / f".{uuid4().hex}.review.wav"
    try:
        rendered = extract_clip(
            source,
            destination,
            start_ms,
            end_ms,
            audio_filter=REVIEW_AUDIO_LOUDNESS_FILTER,
        )
        return rendered.read_bytes()
    finally:
        destination.unlink(missing_ok=True)


def _public_voice_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    clips = []
    for raw in candidate.get("representative_clips", []):
        if not isinstance(raw, dict) or not isinstance(raw.get("media_id"), str):
            continue
        start_ms = _clip_integer(raw.get("start_ms"), 0)
        end_ms = _clip_integer(raw.get("end_ms"), start_ms + 6_000)
        clips.append(
            {
                "media_id": raw["media_id"],
                "start_ms": start_ms,
                "end_ms": max(end_ms, start_ms + 800),
            }
        )
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


def _clip_text(clip: Mapping[str, Any], field: str) -> str:
    value = clip.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError("voice review clip is invalid")
    return value


def _clip_integer(value: object, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return fallback
    return value


def _optional_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


__all__ = [
    "DeviceReviewService",
    "MAX_REVIEW_AUDIO_BYTES",
    "MAX_REVIEW_AUDIO_DURATION_MS",
    "REVIEW_AUDIO_LOUDNESS_FILTER",
]
