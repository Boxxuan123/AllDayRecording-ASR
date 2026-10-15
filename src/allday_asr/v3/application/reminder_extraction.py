from __future__ import annotations
from .generation_inputs import session_inputs, input_snapshot, product_inputs, product_snapshot

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from allday_asr.v3.domain.hashing import canonical_json_sha256
from allday_asr.v3.domain.reminder_time import explicit_task, VERSION

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
        execution_id: str | None = None,
    ) -> dict[str, Any]:
        if self._generator is None:
            raise ReminderGenerationUnavailable("Codex reminder generation is disabled")
        request = replace(self._request(session_id), execution_id=execution_id)
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
                    "input_snapshot": input_snapshot("reminder", request),
                    "utterance_ids": [
                        item["utterance_id"] for item in request.utterances
                    ],
                    "active_event_ids": [
                        item["event_id"] for item in request.active_reminders
                    ],
                    "codex_turn_id": generated.turn_id,
                    "execution_id": generated.execution_id,
                    "reasoning_effort": generated.reasoning_effort.value,
                    "usage": generated.usage,
                },
                intents=intents,
            ),
            allow_auto_apply=False,
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

    def extract_product(self, session_id: str, **_options) -> dict[str, Any]:
        with self._uow_factory().reading() as uow:
            detail = uow.desktop.session_detail(session_id)
            inputs = session_inputs(uow, session_id, 'product-reminder')
            snapshots = {r['utterance_id']: product_snapshot(product_inputs(uow, session_id, r['utterance_id'], detail=detail))
                         for r in inputs['utterances']}
        request = ReminderModelRequest(session_id, inputs['captured_timezone'], _datetime(self._now()),
                                       tuple(inputs['utterances']), ())
        candidates = []
        now = datetime.fromisoformat(request.now_utc.replace("Z", "+00:00"))
        for utterance in request.utterances:
            if utterance["identity"] != "self":
                continue
            parsed = explicit_task(utterance["text"], utterance["start_at"], request.captured_timezone)
            if parsed is None:
                continue
            title, due, expression = parsed
            source = {
                "session_id": session_id, "start_at": utterance["start_at"],
                "end_at": utterance["end_at"], "action": title, "scheduled_at": _datetime(due),
            }
            key = "product-reminder:" + canonical_json_sha256(source)
            # Consult prior decisions even after a task has become overdue.
            with self._uow_factory().reading() as uow:
                recovered = uow.idempotency.response(key + ':recovery')
                prior = uow.idempotency.response(key)
            if recovered:
                candidates.append(self._reminders.candidate(recovered['candidate_id']))
                continue
            if due <= now and prior is None:
                continue
            intent = ReminderIntent(
                operation=ReminderOperation.CREATE_TASK, session_id=session_id,
                actor_person_id="self", commitment_direction=CommitmentDirection.NOT_APPLICABLE,
                title=title, scheduled_at=due, confidence=1.0, needs_confirmation=True,
                evidence_utterance_ids=(utterance["utterance_id"],), reason="explicit_self_future_task",
            )
            result = self._reminders.submit_generation(ReminderGenerationSubmission(
                producer="product-reminder", producer_version=VERSION, model="bounded-rule",
                prompt_version=VERSION, extractor_version=VERSION,
                input_scope={**source, "product_source_key": key,
                    "input_snapshot": snapshots[utterance['utterance_id']],
                    "utterance_ids": [utterance["utterance_id"]],
                    "source_text": utterance["text"], "speaker_identity": "self",
                    "recorded_at": utterance["start_at"], "timezone": request.captured_timezone,
                    "time_expression": expression, "utterance_revision": utterance["revision"]},
                intents=(intent,)), allow_auto_apply=False)
            candidates.extend(result["candidates"])
        return {"status": "succeeded", "candidates": candidates, "extractor_version": VERSION}

    def _request(self, session_id: str) -> ReminderModelRequest:
        with self._uow_factory().reading() as uow:
            inputs = session_inputs(uow, session_id, "reminder")
        utterances = tuple(inputs["utterances"])
        if not utterances:
            raise ValueError("recording session has no active utterances")
        if sum(len(str(value["text"])) for value in utterances) > 200_000:
            raise ValueError("recording session transcript exceeds Codex extraction limit")
        active = tuple(inputs["active_reminders"])
        return ReminderModelRequest(
            session_id=session_id,
            captured_timezone=inputs["captured_timezone"],
            now_utc=_datetime(self._now()),
            utterances=utterances,
            active_reminders=active,
        )

    def inspect_product_recovery(self, candidate_id: str) -> dict:
        from .reminder_product_recovery import recovery_report
        with self._uow_factory().reading() as uow:
            report = recovery_report(uow, candidate_id)
        return {k: v for k, v in report.items() if k != 'current_inputs'}

    def recover_product(self, candidate_id: str, actor: str) -> dict:
        from .reminder_product_recovery import recovery_report
        if not actor.strip():
            raise ValueError('recovery requires an actor')
        with self._uow_factory().reading() as uow:
            report = recovery_report(uow, candidate_id)
            original = uow.reminders.get_candidate(candidate_id)
            generation = uow.knowledge.get_generation(original.generation_id)
            proposal = uow.knowledge.get_proposal(original.proposal_id)
        if report['replacement_candidate_id']:
            return {'candidate': self._reminders.candidate(report['replacement_candidate_id']), 'replayed': True}
        if not report['eligible']:
            raise ValueError('product recovery requires source review: ' + report['reason'])
        inputs = report['current_inputs']
        parsed = explicit_task(inputs['utterance']['text'], inputs['utterance']['start_at'], inputs['captured_timezone'])
        if parsed is None or parsed[1] <= self._now():
            raise ValueError('product recovery requires a current explicit future self task')
        key = report['product_source_key']
        scope = {**generation.input_scope, 'input_snapshot': product_snapshot(inputs),
                 'product_source_key': key + ':attempt:' + candidate_id,
                 'recovery_of_candidate_id': candidate_id, 'recovery_of_generation_id': generation.generation_id,
                 'recovery_actor': actor, 'recovery_reason': report['reason']}
        scope.pop('resolved_inputs', None)
        result = self._reminders.submit_generation(ReminderGenerationSubmission(
            producer='product-reminder', producer_version=VERSION, model='bounded-rule',
            prompt_version=VERSION, extractor_version=VERSION, input_scope=scope,
            intents=(ReminderIntent(operation=original.operation, session_id=original.session_id,
                actor_person_id=original.actor_person_id, commitment_direction=original.commitment_direction,
                title=parsed[0], scheduled_at=parsed[1], related_person_ids=original.related_person_ids,
                location=original.location, confidence=original.confidence, needs_confirmation=True,
                evidence_utterance_ids=proposal.evidence_utterance_ids),)),
            allow_auto_apply=False, _product_recovery=(candidate_id, key))
        return {'candidate': result['candidates'][0], 'replayed': False}


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
