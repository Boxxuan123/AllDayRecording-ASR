from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import time
import wave
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4
from allday_asr.v3.domain.hashing import canonical_json_sha256
from allday_asr.v3.application.speaker_profile_purity import (
    append_review as append_purity_review,
    list_phone_tasks as list_purity_phone_tasks,
)

from allday_asr.v3.adapters.audio.tools import extract_clip
from allday_asr.v3.interfaces.transfer.devices import DeviceConflictError
from .device_review_fields import _public_voice_candidate, _weakest_candidate_first, _required_text, _optional_reason
from .review_audio import audio_plan, concatenate
from .review_evidence import candidate_evidence, candidate_key


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
        self._source_digest: str | None = None
        self._cached_snapshot: dict[str, Any] | None = None
        self._cache_until: float = 0.0

    def snapshot(self, only_review_id: str | None = None) -> dict[str, Any]:
        candidates = self.core.people.list_review_candidates(None, "pending", 500)
        by_prototype = {
            str(candidate["prototype_id"]): candidate for candidate in candidates
        }
        items: list[dict[str, Any]] = []
        shared_review_query = getattr(self.core.desktop, "list_reviews_with_candidates", None)
        raw_reviews = (
            shared_review_query(500, candidates)
            if shared_review_query is not None
            else self.core.desktop.list_reviews(500)
        )
        purity_items: list[dict[str, Any]] = []
        purity_history: list[dict[str, Any]] = []
        factory = getattr(self.core.desktop, "_uow_factory", None)
        if factory is not None:
            with factory().reading() as uow:
                purity_items = list_purity_phone_tasks(uow.desktop.connection)
                purity_history = list_purity_phone_tasks(uow.desktop.connection, history=True)
        confirmed_candidates = self.core.people.list_review_candidates(None, 'confirmed', 500)
        blind = getattr(self.core, 'blind_validation', None)
        blind_items = blind.tasks() if blind else []
        blind_history = blind.tasks(history=True) if blind else []
        source_digest = canonical_json_sha256({
            "pending": candidates, "reviews": raw_reviews,
            "confirmed": confirmed_candidates, "purity": purity_items,
            "purity_history": purity_history,
            'blind': blind_items, 'blind_history': blind_history,
        })
        if (only_review_id is None and time.monotonic() < self._cache_until
                and source_digest == self._source_digest
                and self._cached_snapshot is not None):
            return copy.deepcopy(self._cached_snapshot)
        for raw in raw_reviews:
            if only_review_id is not None and raw.get('review_id') != only_review_id:
                continue
            item = dict(raw)
            context = dict(item.get("context") or {})
            if item.get("kind") == "reminder":
                candidate = self.core.reminders.candidate(str(item["source_id"]))
                context["source"] = candidate["input_scope"]
                context["actor_person_id"] = candidate["actor_person_id"]
                context["source_text"] = candidate["input_scope"].get("source_text") or context.get("source_text", "")
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
        for raw in (*purity_items, *purity_history, *blind_items, *blind_history):
            if only_review_id is None or raw["review_id"] == only_review_id:
                items.append(raw)
        # Reuse the existing per-sample review surface for historical grants.
        # These rows offer withdrawal only, including noncurrent/inactive sources.
        for candidate in confirmed_candidates:
            if only_review_id is not None and only_review_id != f"voice-grant:{candidate['prototype_id']}:{candidate['person_id']}":
                continue
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
        clips_by_session: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            if item["kind"] in {"speaker_profile_purity", 'blind_identity_review'}:
                continue
            for candidate in item.get("context", {}).get("voice_candidates", []):
                clips_by_session.setdefault(str(candidate["session_id"]), []).extend(
                    candidate.get("representative_clips", [])
                )
        details = {}
        lightweight_evidence = getattr(self.core.desktop, "review_evidence_detail", None)
        for session_id, clips in clips_by_session.items():
            try:
                details[session_id] = (
                    lightweight_evidence(session_id, tuple(clips))
                    if lightweight_evidence is not None
                    else self.core.desktop.session_detail(session_id)
                )
            except (KeyError, ValueError, AttributeError):
                details[session_id] = {}
        for item in items:
            for candidate in item.get("context", {}).get("voice_candidates", []):
                candidate.update(self.audio_description(candidate))
                if item["kind"] == "speaker_profile_purity":
                    candidate.update(self._purity_context_description(candidate))
                    candidate["evidence_utterances"] = []
                elif item['kind'] == 'blind_identity_review':
                    candidate['evidence_utterances'] = []
                else:
                    session_id = candidate['session_id']
                    candidate['evidence_utterances'] = candidate_evidence(candidate, details[session_id])
                candidate['review_key'] = candidate_key(candidate)
        snapshot = {"items": items, "version": canonical_json_sha256(items)}
        if only_review_id is None:
            self._source_digest = source_digest
            self._cached_snapshot = copy.deepcopy(snapshot)
            self._cache_until = time.monotonic() + 3.0
        return snapshot

    def resolve(
        self, device_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        review_id = _required_text(payload, "review_id")
        action = _required_text(payload, "action")
        actor = f"phone-device:{device_id}"
        operation_id = payload.get("operation_id")
        if operation_id is not None:
            if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", operation_id):
                raise ValueError("review operation_id is invalid")
            with self.core.desktop._uow_factory().reading() as uow:
                prior = uow.desktop.connection.execute(
                    "SELECT prototype_id, person_id, decision, actor FROM voice_prototype_reviews WHERE review_id=?",
                    (operation_id,),
                ).fetchone()
            if prior is not None:
                decision = {"confirm": "confirmed", "reject": "rejected", "uncertain": "uncertain", "retract": "retracted"}.get(action)
                if (prior["actor"] != actor or prior["prototype_id"] != payload.get("prototype_id")
                        or prior["decision"] != decision):
                    raise ValueError("review operation_id was used for another decision")
                return {"result": {"review_id": operation_id, "decision": decision,
                                   "prototype_id": prior["prototype_id"], "person_id": prior["person_id"]},
                        "reviews": self.snapshot()}
        item = self._current_item(review_id)
        kind = str(item["kind"])
        source_id = str(item["source_id"])
        context = dict(item.get("context") or {})

        if kind == 'blind_identity_review':
            result = self.core.blind_validation.submit(source_id, dict(payload), actor)
        elif kind == "speaker_profile_purity":
            if set(payload) - {"review_id", "action", "primary_speaker_person_id",
                                  "primary_speaker_unknown", "purity", "other_speaker_ids",
                                  "quality_flags", "operation_id"}:
                raise ValueError("purity review fields are invalid")
            with self.core.desktop._uow_factory() as uow:
                result = append_purity_review(uow.desktop.connection, source_id, dict(payload), device_id)
        elif kind == "voice_identity":
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
                expected = payload.get('expected_review_key')
                current = next((c for c in context.get('voice_candidates', [])
                                if c['prototype_id'] == prototype_id), None)
                if expected is not None and (current is None or expected != current.get('review_key')):
                    raise DeviceConflictError('voice sample or authorization changed; reload before deciding')
                result = self.core.people.review_prototype(
                    prototype_id,
                    str(item["person_id"]),
                    decision,
                    note="手机审核",
                    actor=actor,
                    operation_id=operation_id,
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
        self._source_digest = None
        self._cached_snapshot = None
        self._cache_until = 0.0
        return {"result": result, "reviews": self.snapshot()}

    def audio(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if set(payload) - {"review_id", "prototype_id", "audition_key", "audio_content_key", "prefetch", "context"}:
            raise ValueError("audio requests cannot override sample windows")
        review_id = _required_text(payload, "review_id")
        prototype_id = _required_text(payload, "prototype_id")
        item = self._current_item(review_id)
        context = dict(item.get("context") or {})
        if (
            item.get("kind") not in {"voice_identity", "speaker_profile_purity", 'blind_identity_review'}
            or context.get("voice_mode") not in {"known_person", "speaker_discovery", "accepted_grant", "speaker_profile_purity", 'blind_identity_review'}
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
        if payload.get('prefetch', False) not in (True, False):
            raise ValueError('prefetch must be boolean')
        context_requested = payload.get("context", False)
        if type(context_requested) is not bool or (context_requested and item["kind"] != "speaker_profile_purity"):
            raise ValueError("context playback is only supported for purity reviews")
        if context_requested:
            context_candidate, start = self._purity_context_candidate(candidate)
            result = self.render_audio(review_id, context_candidate,
                requested_content_key=payload.get('audio_content_key'),
                prefetch=payload.get('prefetch') is True)
            original = candidate["representative_clips"][0]
            result["target_start_ms"] = original["start_ms"] - start
            result["target_end_ms"] = original["end_ms"] - start
        else:
            result = self.render_audio(review_id, candidate, payload.get("audition_key"),
                                       payload.get('audio_content_key'), prefetch=payload.get('prefetch') is True)
        # Queued/running rendering must not turn an obsolete grant into permission.
        current = self._current_item(review_id)
        current_candidate = next((c for c in current.get('context', {}).get('voice_candidates', [])
                                  if c['prototype_id'] == prototype_id), None)
        if current_candidate is None or current_candidate.get('review_key') != candidate.get('review_key'):
            raise DeviceConflictError('voice evidence changed while preparing audio')
        return result

    def audio_description(self, candidate):
        try:
            plan = audio_plan(candidate)
            plan['audio_content_key'] = self._content_key(plan)
            return {**plan, 'audio_available': True, 'audio_unavailable_reason': ''}
        except (KeyError, ValueError, OSError) as error:
            return {'audio_available': False, 'audio_unavailable_reason': str(error)}

    def _purity_context_candidate(self, candidate):
        original = candidate["representative_clips"][0]
        media = self.core.desktop.media(original["media_id"])
        start = max(0, original["start_ms"] - 4000)
        end = min(int(media["duration_ms"]), original["end_ms"] + 4000)
        return {**candidate, "representative_clips": [{
            "media_id": original["media_id"], "start_ms": start, "end_ms": end,
        }]}, start

    def _purity_context_description(self, candidate):
        try:
            context_candidate, _ = self._purity_context_candidate(candidate)
            return {"context_audio_content_key": self._content_key(audio_plan(context_candidate))}
        except (KeyError, ValueError, OSError):
            return {}

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

    def _content_key(self, plan):
        sources = []
        for window in plan['windows']:
            descriptor = self.core.desktop.media(window['media_id'])
            source = self.core.audio_store.path_for(str(descriptor['storage_key']))
            stat = source.stat()
            if not source.is_file():
                raise FileNotFoundError('原音文件已不可用')
            # storage_key is an immutable SHA-256 address. Stat guards also
            # invalidate manually damaged/replaced replicas without hashing GBs
            # on the HTTP path. No path, receiver address or grant enters the key.
            sources.append([str(descriptor['storage_key']), stat.st_size, stat.st_mtime_ns])
        identity = [sources, plan['windows'], 'review-wav-pcm16-mono-v1', REVIEW_AUDIO_LOUDNESS_FILTER]
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    def render_audio(self, review_id, candidate, requested_key=None, requested_content_key=None, *, prefetch=False):
        plan = audio_plan(candidate)
        if requested_key is not None and requested_key != plan['audition_key']:
            raise DeviceConflictError('sample audio changed; reload before listening')
        content_key = self._content_key(plan)
        if requested_content_key is not None and requested_content_key != content_key:
            raise DeviceConflictError('audio source or processing changed; reload before listening')
        owner = getattr(self.core, 'review_audio_cache', None)
        def render():
            return self._render_bytes(plan)
        if owner is None:
            data, hit = render(), False
        else:
            cache = owner.get(Path(self.core.audio_store.root).parent / 'review-audio-cache')
            data, hit = cache.get(content_key, render, prefetch=prefetch)
        if self._content_key(plan) != content_key:
            raise DeviceConflictError('audio source changed while preparing audio')
        return {**plan, 'review_id': review_id, 'prototype_id': candidate['prototype_id'],
                'audio_content_key': content_key, 'sha256': hashlib.sha256(data).hexdigest(),
                'byte_length': len(data), 'cache_hit': hit,
                'format': 'wav', 'data_base64url': base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii'),
                'start_ms': 0, 'end_ms': plan['total_ms'], 'complete_sample': True}

    def _render_bytes(self, plan):
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
        return data

    def _current_item(self, review_id: str) -> dict[str, Any]:
        if review_id.startswith('blind:'):
            task_id = review_id.removeprefix('blind:')
            blind = self.core.blind_validation
            for item in (*blind.tasks(task_id=task_id), *blind.tasks(history=True, task_id=task_id)):
                for candidate in item['context']['voice_candidates']:
                    candidate.update(self.audio_description(candidate))
                    candidate['evidence_utterances'] = []
                    candidate['review_key'] = candidate_key(candidate)
                return item
            raise DeviceConflictError('blind review is no longer available; refresh inbox')
        if review_id.startswith("purity:"):
            factory = getattr(self.core.desktop, "_uow_factory", None)
            if factory is not None:
                task_id = review_id.removeprefix("purity:")
                with factory().reading() as uow:
                    pending = list_purity_phone_tasks(
                        uow.desktop.connection, task_id=task_id
                    )
                    history = list_purity_phone_tasks(
                        uow.desktop.connection, history=True, task_id=task_id
                    )
                for item in (*pending, *history):
                    if item["review_id"] != review_id:
                        continue
                    for candidate in item["context"]["voice_candidates"]:
                        candidate.update(self.audio_description(candidate))
                        candidate.update(self._purity_context_description(candidate))
                        candidate["evidence_utterances"] = []
                        candidate["review_key"] = candidate_key(candidate)
                    return item
                raise DeviceConflictError("review item is no longer pending")
        for item in self.snapshot(only_review_id=review_id)["items"]:
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
            timeout=30,
        )
        with wave.open(str(rendered), 'rb') as audio:
            duration_ms = audio.getnframes() * 1000 / audio.getframerate()
            if abs(duration_ms - (end_ms - start_ms)) > 1:
                raise ValueError('source audio does not cover the complete sample window')
        return rendered.read_bytes()
    finally:
        destination.unlink(missing_ok=True)




__all__ = [
    "DeviceReviewService",
    "MAX_REVIEW_AUDIO_BYTES",
    "MAX_REVIEW_AUDIO_DURATION_MS",
    "REVIEW_AUDIO_LOUDNESS_FILTER",
]
