from __future__ import annotations
from allday_asr.v3.domain.sound_kind import is_usable_speech

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from allday_asr.v3.domain.ids import stable_ulid
from allday_asr.v3.domain.knowledge import (
    EventKind,
    EventOperationKind,
    GenerationSubmission,
    KnowledgeLayer,
    ProposalKind,
)
from allday_asr.v3.ports.event_generation import (
    SemanticEventModelGenerator,
    SemanticEventModelRequest,
    SemanticEventReasoningEffort,
)
from allday_asr.v3.ports.repositories import UnitOfWork

from .knowledge import KnowledgeArchitectureService


UnitOfWorkFactory = Callable[[], UnitOfWork]
AUTO_ACCEPT_SEMANTIC_EVENT_CONFIDENCE = 0.90
_ALLOWED_EFFORTS = {"auto", "low", "medium", "high", "xhigh"}
_EVENT_FIELDS = {
    "event_kind",
    "title",
    "summary",
    "topics",
    "related_person_ids",
    "person_id",
    "confidence",
    "evidence_utterance_ids",
    "needs_confirmation",
    "reason",
}
_RECORD_EVENT_KINDS = {
    EventKind.DECISION,
    EventKind.PERSON_FACT,
    EventKind.IMPORTANT_EXPERIENCE,
}


class SemanticEventGenerationUnavailable(RuntimeError):
    pass


class SemanticEventGenerationFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class _EventDraft:
    event_id: str
    event_kind: EventKind
    title: str
    summary: str
    topics: tuple[str, ...]
    related_person_ids: tuple[str, ...]
    person_id: str | None
    confidence: float
    evidence_utterance_ids: tuple[str, ...]
    needs_confirmation: bool
    reason: str

    def proposal(
        self, session_id: str
    ) -> tuple[ProposalKind, dict[str, Any], tuple[str, ...]]:
        patch: dict[str, Any] = {
            "title": self.title,
            "summary": self.summary,
            "topics": list(self.topics),
            "related_person_ids": list(self.related_person_ids),
            "confidence": self.confidence,
            "extraction_reason": self.reason,
        }
        if self.person_id is not None:
            patch["person_id"] = self.person_id
        return (
            ProposalKind.EVENT_OPERATION,
            {
                "event_id": self.event_id,
                "operation": EventOperationKind.CREATE.value,
                "session_id": session_id,
                "event_kind": self.event_kind.value,
                "expected_revision": 0,
                "patch": patch,
            },
            self.evidence_utterance_ids,
        )


class SemanticEventExtractionService:
    """Turns the native transcript into reviewed, evidence-bound V3 events."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        knowledge: KnowledgeArchitectureService,
        generator: SemanticEventModelGenerator | None,
        *,
        default_effort: str = "auto",
        allow_auto_accept: bool = True,
    ) -> None:
        if default_effort not in _ALLOWED_EFFORTS:
            raise ValueError("default semantic event reasoning effort is invalid")
        self._uow_factory = uow_factory
        self._knowledge = knowledge
        self._generator = generator
        self._default_effort = default_effort
        self._allow_auto_accept = allow_auto_accept

    @property
    def enabled(self) -> bool:
        return self._generator is not None

    def extract(
        self,
        session_id: str,
        *,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        if self._generator is None:
            raise SemanticEventGenerationUnavailable(
                "Codex semantic event generation is disabled"
            )
        request = self._request(session_id)
        effort = select_semantic_event_reasoning_effort(
            request,
            reasoning_effort or self._default_effort,
        )
        try:
            generated = self._generator.generate(request, effort)
        except Exception as exc:
            raise SemanticEventGenerationFailed(
                "Codex could not generate semantic events"
            ) from exc
        try:
            drafts = tuple(_event(value, request) for value in generated.events)
            _assert_distinct_drafts(drafts)
        except (TypeError, ValueError) as exc:
            raise SemanticEventGenerationFailed(
                "Codex returned semantic events that failed validation"
            ) from exc

        known_ids = self._known_event_ids(session_id)
        selected = tuple(value for value in drafts if value.event_id not in known_ids)
        receipt = self._knowledge.submit_generation(
            GenerationSubmission(
                layer=KnowledgeLayer.EVENT,
                producer="codex-semantic-event-generator",
                producer_version=self._generator.producer_version,
                model=self._generator.model_label,
                prompt_version=self._generator.prompt_version,
                extractor_version=self._generator.extractor_version,
                input_scope={
                    "session_id": request.session_id,
                    "utterance_ids": [
                        item["utterance_id"] for item in request.utterances
                    ],
                    "codex_turn_id": generated.turn_id,
                    "reasoning_effort": generated.reasoning_effort.value,
                    "usage": generated.usage,
                },
                proposals=tuple(value.proposal(session_id) for value in selected),
            ),
            allow_empty=True,
        )

        events: list[dict[str, Any]] = []
        for draft, proposal in zip(selected, receipt["proposals"], strict=True):
            status = "pending_review"
            revision: int | None = None
            if self._auto_accept_allowed(draft):
                resolution = self._knowledge.accept_proposal(
                    str(proposal["proposal_id"]),
                    "semantic-event-policy",
                )
                status = "accepted"
                revision = resolution.resource_revision
            events.append(
                {
                    "event_id": draft.event_id,
                    "event_kind": draft.event_kind.value,
                    "title": draft.title,
                    "confidence": draft.confidence,
                    "needs_confirmation": draft.needs_confirmation,
                    "proposal_id": proposal["proposal_id"],
                    "status": status,
                    "event_revision": revision,
                }
            )
        receipt["semantic_events"] = events
        receipt["duplicate_count"] = len(drafts) - len(selected)
        receipt["auto_accepted_count"] = sum(
            value["status"] == "accepted" for value in events
        )
        receipt["pending_review_count"] = sum(
            value["status"] == "pending_review" for value in events
        )
        receipt["codex"] = {
            "turn_id": generated.turn_id,
            "model": self._generator.model_label,
            "reasoning_effort": generated.reasoning_effort.value,
            "usage": generated.usage,
        }
        return receipt

    def close(self) -> None:
        if self._generator is not None:
            self._generator.close()

    def _request(self, session_id: str) -> SemanticEventModelRequest:
        with self._uow_factory() as uow:
            detail = uow.desktop.session_detail(session_id)
        speaker_references = {
            str(value["speaker_track_id"]): value for value in detail["speaker_tracks"]
        }
        utterances = tuple(
            _utterance(
                value,
                speaker_references.get(str(value.get("speaker_track_id"))),
            )
            for value in detail["utterances"]
            if value.get("status") == "active" and str(value.get("text", "")).strip()
            and is_usable_speech(value.get("evidence", {}))
        )
        if not utterances:
            raise ValueError("recording session has no active utterances")
        if sum(len(str(value["text"])) for value in utterances) > 200_000:
            raise ValueError(
                "recording session transcript exceeds Codex extraction limit"
            )
        return SemanticEventModelRequest(
            session_id=session_id,
            captured_timezone=str(detail["session"].get("timezone") or "UTC"),
            utterances=utterances,
        )

    def _known_event_ids(self, session_id: str) -> set[str]:
        event_ids = {
            str(value["event_id"]) for value in self._knowledge.list_events(session_id)
        }
        for proposal in self._knowledge.list_proposals(limit=500):
            payload = proposal.get("payload")
            if not isinstance(payload, dict) or payload.get("session_id") != session_id:
                continue
            event_id = payload.get("event_id")
            if isinstance(event_id, str) and event_id:
                event_ids.add(event_id)
        return event_ids

    def _auto_accept_allowed(self, draft: _EventDraft) -> bool:
        return (
            self._allow_auto_accept
            and draft.event_kind in _RECORD_EVENT_KINDS
            and not draft.needs_confirmation
            and draft.confidence >= AUTO_ACCEPT_SEMANTIC_EVENT_CONFIDENCE
        )


def select_semantic_event_reasoning_effort(
    request: SemanticEventModelRequest,
    requested: str,
) -> SemanticEventReasoningEffort:
    value = requested.strip().lower()
    if value not in _ALLOWED_EFFORTS:
        raise ValueError("reasoning_effort must be auto, low, medium, high or xhigh")
    if value != "auto":
        return SemanticEventReasoningEffort(value)
    text_size = sum(len(str(item["text"])) for item in request.utterances)
    if len(request.utterances) > 120 or text_size > 15_000:
        return SemanticEventReasoningEffort.HIGH
    if len(request.utterances) <= 12 and text_size <= 1_200:
        return SemanticEventReasoningEffort.LOW
    return SemanticEventReasoningEffort.MEDIUM


def _event(value: dict[str, Any], request: SemanticEventModelRequest) -> _EventDraft:
    if not isinstance(value, dict) or set(value) != _EVENT_FIELDS:
        raise ValueError("Codex semantic event fields are invalid")
    event_kind = EventKind(_string(value["event_kind"], "event_kind"))
    if event_kind not in _RECORD_EVENT_KINDS:
        raise ValueError("Codex semantic event kind is not record-only")
    title = _string(value["title"], "title")
    summary = _string(value["summary"], "summary")
    topics = _string_tuple(value["topics"], "topics")
    related = _string_tuple(value["related_person_ids"], "related_person_ids")
    evidence_ids = _string_tuple(
        value["evidence_utterance_ids"], "evidence_utterance_ids"
    )
    allowed_evidence = {str(item["utterance_id"]) for item in request.utterances}
    if not evidence_ids or not set(evidence_ids).issubset(allowed_evidence):
        raise ValueError("Codex semantic event evidence is outside the request")
    allowed_people = {
        str(item["speaker_person_id"])
        for item in request.utterances
        if item.get("speaker_person_id")
    }
    if not set(related).issubset(allowed_people):
        raise ValueError("Codex semantic event person is outside the request")
    person_id = _optional_string(value["person_id"], "person_id")
    if person_id is not None and person_id not in allowed_people:
        raise ValueError("Codex semantic event subject is outside the request")
    if event_kind is EventKind.PERSON_FACT and person_id is None:
        raise ValueError("Codex person fact requires a known person")
    confidence = value["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("Codex semantic event confidence is invalid")
    confidence = float(confidence)
    if not 0 <= confidence <= 1:
        raise ValueError("Codex semantic event confidence is outside 0..1")
    needs_confirmation = value["needs_confirmation"]
    if not isinstance(needs_confirmation, bool):
        raise ValueError("Codex semantic event confirmation flag is invalid")
    reason = _string(value["reason"], "reason")
    event_id = stable_ulid(
        "codex-semantic-event",
        request.session_id,
        event_kind.value,
        title.casefold(),
        "\0".join(evidence_ids),
    )
    return _EventDraft(
        event_id=event_id,
        event_kind=event_kind,
        title=title,
        summary=summary,
        topics=topics,
        related_person_ids=related,
        person_id=person_id,
        confidence=confidence,
        evidence_utterance_ids=evidence_ids,
        needs_confirmation=needs_confirmation,
        reason=reason,
    )


def _assert_distinct_drafts(values: tuple[_EventDraft, ...]) -> None:
    event_ids = [value.event_id for value in values]
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("Codex returned duplicate semantic events")


def _utterance(
    value: dict[str, Any],
    speaker_reference: dict[str, Any] | None,
) -> dict[str, Any]:
    if "person" in value.get("evidence", {}).get("annotation_review", {}).get("dimensions", {}):
        speaker_reference = None
        value = {**value, "identity": "unknown", "speaker_label": None, "speaker_track_id": None}
    speaker_reference = speaker_reference or {}
    annotation = value.get("evidence", {}).get("person_annotation", {})
    if annotation.get("person_id") and "person" not in value.get("evidence", {}).get("annotation_review", {}).get("dimensions", {}):
        person_id = annotation["person_id"]
        speaker_reference = {**speaker_reference, "person_id": person_id,
            "person_name": speaker_reference.get("person_name") if speaker_reference.get("person_id") == person_id else None}
    return {
        "utterance_id": str(value["utterance_id"]),
        "start_at": str(value["start_at"]),
        "end_at": str(value["end_at"]),
        "speaker_label": value.get("speaker_label"),
        "speaker_person_id": speaker_reference.get("person_id"),
        "speaker_name": speaker_reference.get("person_name"),
        "identity": str(value.get("identity") or "unknown"),
        "text": str(value["text"]),
        "revision": int(value["revision"]),
    }


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Codex semantic event {name} is invalid")
    return value.strip()


def _optional_string(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _string(value, name)


def _string_tuple(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"Codex semantic event {name} is invalid")
    result = tuple(_string(item, name) for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"Codex semantic event {name} contains duplicates")
    return result


__all__ = [
    "AUTO_ACCEPT_SEMANTIC_EVENT_CONFIDENCE",
    "SemanticEventExtractionService",
    "SemanticEventGenerationFailed",
    "SemanticEventGenerationUnavailable",
    "select_semantic_event_reasoning_effort",
]
