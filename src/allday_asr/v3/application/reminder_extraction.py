from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from allday_asr.v3.domain.reminders import (
    CommitmentDirection,
    ReminderGenerationSubmission,
    ReminderIntent,
    ReminderOperation,
)
from allday_asr.v3.ports.reminder_generation import (
    ReminderModelGenerator,
    ReminderModelRequest,
    ReminderReasoningEffort,
)
from allday_asr.v3.ports.repositories import UnitOfWork

from .reminders import IntelligentReminderService


UnitOfWorkFactory = Callable[[], UnitOfWork]
DateTimeClock = Callable[[], datetime]
_ALLOWED_EFFORTS = {"auto", "low", "medium", "high", "xhigh"}
_MUTATION_CUES = (
    "改成",
    "改到",
    "改为",
    "取消",
    "不用",
    "不去了",
    "说错",
    "已经",
    "完成",
    "done",
    "cancel",
    "instead",
    "reschedule",
)
_INTENT_FIELDS = {
    "operation",
    "title",
    "actor_person_id",
    "commitment_direction",
    "related_person_ids",
    "scheduled_at",
    "location",
    "confidence",
    "evidence_utterance_ids",
    "needs_confirmation",
    "target_event_id",
    "expected_revision",
    "reason",
}


class ReminderGenerationUnavailable(RuntimeError):
    pass


class ReminderGenerationFailed(RuntimeError):
    pass


class ReminderExtractionService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        reminders: IntelligentReminderService,
        generator: ReminderModelGenerator | None,
        *,
        default_effort: str = "auto",
        allow_auto_apply: bool = False,
        now: DateTimeClock | None = None,
    ) -> None:
        if default_effort not in _ALLOWED_EFFORTS:
            raise ValueError("default reminder reasoning effort is invalid")
        self._uow_factory = uow_factory
        self._reminders = reminders
        self._generator = generator
        self._default_effort = default_effort
        self._allow_auto_apply = allow_auto_apply
        self._now = now or _utc_now

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
            raise ReminderGenerationUnavailable("Codex reminder generation is disabled")
        request = self._request(session_id)
        effort = select_reasoning_effort(
            request,
            reasoning_effort or self._default_effort,
        )
        try:
            generated = self._generator.generate(request, effort)
        except Exception as exc:
            raise ReminderGenerationFailed(
                "Codex could not generate reminder candidates"
            ) from exc
        try:
            intents = tuple(
                _intent(value, request) for value in generated.intents
            )
        except (TypeError, ValueError) as exc:
            raise ReminderGenerationFailed(
                "Codex returned reminder candidates that failed validation"
            ) from exc
        result = self._reminders.submit_generation(
            ReminderGenerationSubmission(
                producer="codex-reminder-generator",
                producer_version=self._generator.producer_version,
                model=self._generator.model_label,
                prompt_version=self._generator.prompt_version,
                extractor_version=self._generator.extractor_version,
                input_scope={
                    "session_id": request.session_id,
                    "utterance_ids": [
                        item["utterance_id"] for item in request.utterances
                    ],
                    "active_event_ids": [
                        item["event_id"] for item in request.active_reminders
                    ],
                    "codex_turn_id": generated.turn_id,
                    "reasoning_effort": generated.reasoning_effort.value,
                    "usage": generated.usage,
                },
                intents=intents,
            ),
            allow_auto_apply=self._allow_auto_apply,
            allow_empty=True,
        )
        result["codex"] = {
            "turn_id": generated.turn_id,
            "model": self._generator.model_label,
            "reasoning_effort": generated.reasoning_effort.value,
            "usage": generated.usage,
        }
        return result

    def close(self) -> None:
        if self._generator is not None:
            self._generator.close()

    def _request(self, session_id: str) -> ReminderModelRequest:
        with self._uow_factory() as uow:
            detail = uow.desktop.session_detail(session_id)
            scheduled = uow.reminders.list_schedules(
                status="scheduled",
                session_id=session_id,
                due_before=None,
                limit=100,
            )
            delivered = uow.reminders.list_schedules(
                status="delivered",
                session_id=session_id,
                due_before=None,
                limit=100,
            )
        utterances = tuple(
            _utterance(value)
            for value in detail["utterances"]
            if value.get("status") == "active" and str(value.get("text", "")).strip()
        )
        if not utterances:
            raise ValueError("recording session has no active utterances")
        if sum(len(str(value["text"])) for value in utterances) > 200_000:
            raise ValueError("recording session transcript exceeds Codex extraction limit")
        active = tuple(_active_reminder(value) for value in (*scheduled, *delivered))
        return ReminderModelRequest(
            session_id=session_id,
            captured_timezone=str(detail["session"].get("timezone") or "UTC"),
            now_utc=_datetime(self._now()),
            utterances=utterances,
            active_reminders=active,
        )


def select_reasoning_effort(
    request: ReminderModelRequest, requested: str
) -> ReminderReasoningEffort:
    value = requested.strip().lower()
    if value not in _ALLOWED_EFFORTS:
        raise ValueError("reasoning_effort must be auto, low, medium, high or xhigh")
    if value != "auto":
        return ReminderReasoningEffort(value)
    text = "\n".join(str(item["text"]) for item in request.utterances)
    if (
        len(request.utterances) > 120
        or len(text) > 15_000
        or (
            request.active_reminders
            and any(cue in text.casefold() for cue in _MUTATION_CUES)
        )
    ):
        return ReminderReasoningEffort.HIGH
    if (
        len(request.utterances) <= 12
        and len(text) <= 1_200
        and not request.active_reminders
    ):
        return ReminderReasoningEffort.LOW
    return ReminderReasoningEffort.MEDIUM


def _intent(value: dict[str, Any], request: ReminderModelRequest) -> ReminderIntent:
    if not isinstance(value, dict) or set(value) != _INTENT_FIELDS:
        raise ValueError("Codex reminder intent fields are invalid")
    operation = ReminderOperation(_string(value["operation"], "operation"))
    if operation is ReminderOperation.IGNORE:
        raise ValueError("Codex cannot emit IGNORE")
    evidence_ids = _string_tuple(
        value["evidence_utterance_ids"], "evidence_utterance_ids"
    )
    allowed_evidence = {str(item["utterance_id"]) for item in request.utterances}
    if not evidence_ids or not set(evidence_ids).issubset(allowed_evidence):
        raise ValueError("Codex reminder evidence is outside the request")
    target = _optional_string(value["target_event_id"], "target_event_id")
    revision = value["expected_revision"]
    if type(revision) is not int:
        raise ValueError("Codex reminder revision is invalid")
    if operation in {
        ReminderOperation.UPDATE_EVENT,
        ReminderOperation.CANCEL_EVENT,
        ReminderOperation.MARK_DONE,
    }:
        matches = [
            item
            for item in request.active_reminders
            if item["event_id"] == target and item["event_revision"] == revision
        ]
        if len(matches) != 1:
            raise ValueError("Codex reminder target is not an active event revision")
    confidence = value["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("Codex reminder confidence is invalid")
    needs_confirmation = value["needs_confirmation"]
    if not isinstance(needs_confirmation, bool):
        raise ValueError("Codex reminder confirmation flag is invalid")
    return ReminderIntent(
        operation=operation,
        session_id=request.session_id,
        title=_optional_string(value["title"], "title"),
        actor_person_id=_string(value["actor_person_id"], "actor_person_id"),
        commitment_direction=CommitmentDirection(
            _string(value["commitment_direction"], "commitment_direction")
        ),
        related_person_ids=_string_tuple(
            value["related_person_ids"], "related_person_ids"
        ),
        scheduled_at=_optional_datetime(value["scheduled_at"]),
        location=_optional_string(value["location"], "location"),
        confidence=float(confidence),
        evidence_utterance_ids=evidence_ids,
        needs_confirmation=needs_confirmation,
        target_event_id=target,
        expected_revision=revision,
        reason=_optional_string(value["reason"], "reason"),
    )


def _utterance(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "utterance_id": str(value["utterance_id"]),
        "start_at": str(value["start_at"]),
        "end_at": str(value["end_at"]),
        "speaker_label": value.get("speaker_label"),
        "identity": str(value.get("identity") or "unknown"),
        "text": str(value["text"]),
        "revision": int(value["revision"]),
    }


def _active_reminder(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(value["event_id"]),
        "event_revision": int(value["event_revision"]),
        "title": str(value["title"]),
        "actor_person_id": str(value["actor_person_id"]),
        "commitment_direction": str(value["commitment_direction"]),
        "related_person_ids": list(value["related_person_ids"]),
        "scheduled_at": str(value["scheduled_at"]),
        "location": value["location"],
        "status": str(value["status"]),
    }


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Codex reminder {name} is invalid")
    return value.strip()


def _optional_string(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _string(value, name)


def _string_tuple(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"Codex reminder {name} is invalid")
    result = tuple(_string(item, name) for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"Codex reminder {name} contains duplicates")
    return result


def _optional_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Codex reminder scheduled_at is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Codex reminder scheduled_at is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Codex reminder scheduled_at requires a timezone")
    return parsed.astimezone(timezone.utc)


def _datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "ReminderExtractionService",
    "ReminderGenerationFailed",
    "ReminderGenerationUnavailable",
    "select_reasoning_effort",
]
