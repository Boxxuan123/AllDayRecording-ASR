from __future__ import annotations

import base64
import wave
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from allday_asr.v3.adapters.audio.tools import extract_clip
from allday_asr.v3.interfaces.transfer.devices import DeviceConflictError
from .review_audio import audio_plan, concatenate


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
            if item.get("kind") == "voice_identity" and context.get("voice_mode") == "speaker_discovery":
                detail = self.core.people.cluster(str(item["source_id"]))
                members = {m["speaker_track_id"]: m["session_id"] for m in detail["members"]}
                context["voice_candidates"] = [_public_voice_candidate({
                    **p, "session_id": members.get(p["speaker_track_id"], ""),
                    "person_id": "", "person_name": "未知人物",
                }) for p in detail["prototypes"] if p["status"] != "revoked"]
                context["prototype_ids"] = [p["prototype_id"] for p in context["voice_candidates"]]
                if not context["voice_candidates"]:
                    continue
            item["context"] = context
            items.append(item)
        # Reuse the existing per-sample review surface for historical grants.
        # These rows offer withdrawal only, including noncurrent/inactive sources.
        for candidate in self.core.people.list_review_candidates(None, 'confirmed', 500):
            items.append({
                'review_id': f"voice-grant:{candidate['prototype_id']}:{candidate['person_id']}",
                'kind': 'voice_identity', 'priority': 'normal',
                'source_id': candidate['cluster_id'], 'source_revision': None,
                'session_id': candidate['session_id'], 'person_id': candidate['person_id'],
                'title': candidate['person_name'], 'summary': '已采纳样本授权，可单独撤回',
                'reason': 'voice_grant_management', 'evidence_count': len(candidate['representative_clips']),
                'created_at': candidate['created_at'], 'updated_at': candidate['created_at'],
                'context': {'voice_mode': 'accepted_grant', 'review_lane': 'history',
                    'prototype_ids': [candidate['prototype_id']],
                    'voice_candidates': [_public_voice_candidate(candidate)]},
            })
        for item in items:
            for candidate in item.get("context", {}).get("voice_candidates", []):
                candidate.update(self.audio_description(candidate))
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
                if context.get('voice_mode') == 'accepted_grant' and action != 'retract':
                    raise ValueError('historical sample authorization only supports withdrawal')
                decision = {
                    "confirm": "confirmed",
                    "reject": "rejected",
                    "uncertain": "uncertain",
                    "retract": "retracted",
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
        if set(payload) - {"review_id", "prototype_id", "audition_key"}:
            raise ValueError("audio requests cannot override sample windows")
        review_id = _required_text(payload, "review_id")
        prototype_id = _required_text(payload, "prototype_id")
        item = self._current_item(review_id)
        context = dict(item.get("context") or {})
        if (
            item.get("kind") != "voice_identity"
            or context.get("voice_mode") not in {"known_person", "speaker_discovery", "accepted_grant"}
            or prototype_id
            not in {str(value) for value in context.get("prototype_ids", [])}
        ):
            raise DeviceConflictError("voice sample no longer belongs to this review")
        candidates = context.get("voice_candidates", [])
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
        return self.render_audio(review_id, candidate, payload.get("audition_key"))

    def audio_description(self, candidate):
        try:
            plan = audio_plan(candidate)
            for window in plan['windows']:
                descriptor = self.core.desktop.media(window['media_id'])
                if not self.core.audio_store.path_for(str(descriptor['storage_key'])).is_file():
                    raise FileNotFoundError('原音文件已不可用')
            return {**plan, 'audio_available': True, 'audio_unavailable_reason': ''}
        except (KeyError, ValueError, OSError) as error:
            return {'audio_available': False, 'audio_unavailable_reason': str(error)}

    def desktop_audio(self, payload):
        if set(payload) - {'prototype_id', 'person_id', 'describe', 'audition_key'}:
            raise ValueError('audio requests cannot override sample windows')
        prototype_id = _required_text(payload, 'prototype_id')
        person_id = _required_text(payload, 'person_id')
        candidate = next((c for c in self.core.people.list_review_candidates(person_id, None, 500)
                          if c['prototype_id'] == prototype_id and c['person_id'] == person_id), None)
        if candidate is None:
            raise DeviceConflictError('voice sample no longer belongs to this person')
        if payload.get('describe') is True:
            return self.audio_description(candidate)
        return self.render_audio(f'person:{person_id}', candidate, payload.get('audition_key'))

    def render_audio(self, review_id, candidate, requested_key=None):
        plan = audio_plan(candidate)
        if requested_key is not None and requested_key != plan['audition_key']:
            raise DeviceConflictError('sample audio changed; reload before listening')
        parts = []
        temp_root = Path(self.core.audio_store.root).parent / 'review-audio-temp'
        for window in plan['windows']:
            descriptor = self.core.desktop.media(window['media_id'])
            source = self.core.audio_store.path_for(str(descriptor['storage_key']))
            for start in range(window['start_ms'], window['end_ms'], MAX_REVIEW_AUDIO_DURATION_MS):
                parts.append(_normalized_review_audio(source, start,
                    min(window['end_ms'], start + MAX_REVIEW_AUDIO_DURATION_MS), temp_root=temp_root))
                if sum(map(len, parts)) > MAX_REVIEW_AUDIO_BYTES:
                    raise ValueError('normalized voice review audio is too large for mobile playback')
        data = concatenate(parts)
        if not data or len(data) > MAX_REVIEW_AUDIO_BYTES:
            raise ValueError('normalized voice review audio is too large for mobile playback')
        return {**plan, 'review_id': review_id, 'prototype_id': candidate['prototype_id'],
                'format': 'wav', 'data_base64url': base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii'),
                'start_ms': 0, 'end_ms': plan['total_ms'], 'complete_sample': True}

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
        with wave.open(str(rendered), 'rb') as audio:
            duration_ms = audio.getnframes() * 1000 / audio.getframerate()
            if abs(duration_ms - (end_ms - start_ms)) > 1:
                raise ValueError('source audio does not cover the complete sample window')
        return rendered.read_bytes()
    finally:
        destination.unlink(missing_ok=True)


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


__all__ = [
    "DeviceReviewService",
    "MAX_REVIEW_AUDIO_BYTES",
    "MAX_REVIEW_AUDIO_DURATION_MS",
    "REVIEW_AUDIO_LOUDNESS_FILTER",
]
