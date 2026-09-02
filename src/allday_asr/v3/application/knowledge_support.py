from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.knowledge import (
    EventKind,
    EventOperationKind,
    GenerationRecord,
    GenerationSubmission,
    InvalidationEvent,
    KnowledgeLayer,
    MemoryKind,
    ProposalKind,
    ProposalStatus,
    RecomputeRequest,
    StructuredProposal,
)
from allday_asr.v3.ports.repositories import UnitOfWork

UnitOfWorkFactory = Callable[[], UnitOfWork]
DateTimeClock = Callable[[], datetime]
ProposalCreatedHook = Callable[
    [UnitOfWork, GenerationRecord, StructuredProposal, int, datetime], None
]
ProposalAcceptedHook = Callable[
    [UnitOfWork, StructuredProposal, "ProposalResolution", datetime], None
]
ProposalRejectedHook = Callable[[UnitOfWork, StructuredProposal, datetime], None]


@dataclass(frozen=True)
class ProposalResolution:
    proposal_id: str
    status: ProposalStatus
    resource_type: str | None
    resource_id: str | None
    resource_revision: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "status": self.status.value,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "resource_revision": self.resource_revision,
        }


def cascade_derivations(
    uow: UnitOfWork,
    *,
    source_type: str,
    source_id: str,
    source_revision: int,
    reason: str,
    now: datetime,
) -> tuple[InvalidationEvent, ...]:
    root_id = new_ulid()
    created: list[InvalidationEvent] = []
    for target_type, target_id, target_revision in uow.derivations.dependent_closure(
        source_type, source_id
    ):
        event = InvalidationEvent(
            invalidation_id=new_ulid(),
            target_type=target_type,
            target_id=target_id,
            target_revision=target_revision,
            status="stale",
            reason=reason,
            source_type=source_type,
            source_id=source_id,
            source_revision=source_revision,
            cascade_root_id=root_id,
            created_at=now,
        )
        if not uow.derivations.add_invalidation(event):
            continue
        created.append(event)
        if target_type == "event" and uow.reminders.mark_stale(
            target_id, target_revision, _datetime(now)
        ):
            uow.changes.append(
                "reminder",
                target_id,
                target_revision,
                "tombstone",
                None,
            )
        if target_type in {"event", "memory"}:
            uow.derivations.add_recompute_request(
                RecomputeRequest(
                    request_id=new_ulid(),
                    target_type=target_type,
                    target_id=target_id,
                    target_revision=target_revision,
                    invalidation_id=event.invalidation_id,
                    status="queued",
                    reason=reason,
                    created_at=now,
                    updated_at=now,
                )
            )
    return tuple(created)


def _validate_submission(
    submission: GenerationSubmission, *, allow_empty: bool = False
) -> None:
    if not isinstance(submission.input_scope, dict) or not submission.input_scope:
        raise ValueError("generation input scope is required")
    if "resolved_inputs" in submission.input_scope:
        raise ValueError("resolved_inputs is reserved for the server")
    if not submission.proposals and not allow_empty:
        raise ValueError("generation must contain at least one proposal")
    if submission.layer is KnowledgeLayer.EVIDENCE:
        raise ValueError("evidence projections are not submitted as model proposals")
    for kind, payload, evidence_ids in submission.proposals:
        if not isinstance(kind, ProposalKind) or not isinstance(payload, dict):
            raise ValueError("generation proposal is invalid")
        if any(not isinstance(value, str) or not value for value in evidence_ids):
            raise ValueError("proposal evidence ids are invalid")
        if kind is ProposalKind.EVENT_OPERATION:
            _validate_event_proposal(payload, evidence_ids)
        else:
            _validate_memory_proposal(payload)
    if submission.layer is KnowledgeLayer.EVENT and any(
        kind is not ProposalKind.EVENT_OPERATION for kind, _, _ in submission.proposals
    ):
        raise ValueError("event generation can only submit event operations")
    if submission.layer is KnowledgeLayer.MEMORY and any(
        kind is not ProposalKind.MEMORY_RECORD for kind, _, _ in submission.proposals
    ):
        raise ValueError("memory generation can only submit memory records")


def _validate_event_proposal(
    payload: dict[str, Any], evidence_ids: tuple[str, ...]
) -> None:
    required = {
        "operation",
        "session_id",
        "event_kind",
        "expected_revision",
        "patch",
    }
    allowed = required | {"event_id"}
    if set(payload) - allowed or not required.issubset(payload):
        raise ValueError("event proposal fields are invalid")
    operation = EventOperationKind(payload["operation"])
    EventKind(payload["event_kind"])
    revision = payload["expected_revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("event expected revision is invalid")
    if operation is EventOperationKind.CREATE and revision != 0:
        raise ValueError("event create expected revision must be zero")
    if operation is not EventOperationKind.CREATE and revision < 1:
        raise ValueError("event mutation expected revision must be positive")
    if not isinstance(payload["session_id"], str) or not payload["session_id"]:
        raise ValueError("event session id is required")
    if "event_id" in payload and (
        not isinstance(payload["event_id"], str) or not payload["event_id"]
    ):
        raise ValueError("event id is invalid")
    if operation is not EventOperationKind.CREATE and "event_id" not in payload:
        raise ValueError("event mutation requires event id")
    if not isinstance(payload["patch"], dict):
        raise ValueError("event patch must be an object")
    if not evidence_ids:
        raise ValueError("every event operation requires utterance evidence")


def _validate_memory_proposal(payload: dict[str, Any]) -> None:
    required = {
        "session_id",
        "memory_kind",
        "subject_type",
        "subject_id",
        "content",
        "input_event_ids",
    }
    allowed = required | {"memory_id"}
    if set(payload) - allowed or not required.issubset(payload):
        raise ValueError("memory proposal fields are invalid")
    MemoryKind(payload["memory_kind"])
    if payload["session_id"] is not None and (
        not isinstance(payload["session_id"], str) or not payload["session_id"]
    ):
        raise ValueError("memory session id is invalid")
    for key in ("subject_type", "subject_id"):
        if not isinstance(payload[key], str) or not payload[key]:
            raise ValueError("memory subject is invalid")
    if not isinstance(payload["content"], dict) or not payload["content"]:
        raise ValueError("memory content is required")
    event_ids = payload["input_event_ids"]
    if (
        not isinstance(event_ids, list)
        or not event_ids
        or any(not isinstance(value, str) or not value for value in event_ids)
        or len(set(event_ids)) != len(event_ids)
    ):
        raise ValueError("memory input events are invalid")
    if "memory_id" in payload and (
        not isinstance(payload["memory_id"], str) or not payload["memory_id"]
    ):
        raise ValueError("memory id is invalid")


def _merge_patch(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = dict(current)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_patch(result[key], value)
        else:
            result[key] = value
    return result


def _datetime(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
