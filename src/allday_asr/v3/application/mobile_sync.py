from __future__ import annotations

import logging
import time
import hashlib
import json
import re
from dataclasses import replace
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol

from .sound_annotation import classify_segments
from .speaker_annotation import AnnotationValidationError, assign_annotation
from allday_asr.v3.contracts import validate_reminder_dto, validate_utterance_dto
from allday_asr.v3.application.durable_processing import (
    CorrectUtteranceCommand,
    UtteranceRevisionConflict,
    apply_utterance_correction,
)
from allday_asr.v3.domain.device_sync import (
    PROJECTION_VERSION,
    ClientOperation,
    ClientOperationStatus,
    OperationReceipt,
    SyncChange,
    SyncRequest,
    SyncResponse,
)
from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.identity import SelfIdentity
from allday_asr.v3.ports.repositories import UnitOfWork


UnitOfWorkFactory = Callable[[], UnitOfWork]
DateTimeClock = Callable[[], datetime]
_CURSOR = re.compile(r"^cursor-(0|[1-9][0-9]*)$")


class ClientOperationHandler(Protocol):
    def apply(
        self, operation: ClientOperation, uow: UnitOfWork
    ) -> OperationReceipt: ...


class RejectingClientOperationHandler:
    def apply(
        self, operation: ClientOperation, uow: UnitOfWork
    ) -> OperationReceipt:
        del uow
        return OperationReceipt(
            operation_id=operation.operation_id,
            status=ClientOperationStatus.REJECTED,
            resource_revision=None,
            error=_error(
                "UNSUPPORTED_OPERATION",
                f"unsupported client operation: {operation.kind}",
            ),
        )


class UtteranceCorrectionOperationHandler:
    """Apply Phone outbox corrections through the same Core transaction."""

    def __init__(self, *, now: DateTimeClock | None = None) -> None:
        self._now = now or (lambda: datetime.now(timezone.utc))

    def apply(
        self, operation: ClientOperation, uow: UnitOfWork
    ) -> OperationReceipt:
        if operation.kind in {"speaker.assign", "segment.classify"}:
            return self._assign_speaker(operation, uow)
        if operation.kind != "utterance.correct":
            return RejectingClientOperationHandler().apply(operation, uow)
        payload = operation.payload
        if (
            operation.base_revision is None
            or set(payload) != {
                "utterance_id", "text", "speaker_track_id", "identity"
            }
            or not isinstance(payload["utterance_id"], str)
            or not isinstance(payload["text"], str)
            or (
                payload["speaker_track_id"] is not None
                and not isinstance(payload["speaker_track_id"], str)
            )
            or payload["identity"] not in {value.value for value in SelfIdentity}
        ):
            return OperationReceipt(
                operation_id=operation.operation_id,
                status=ClientOperationStatus.REJECTED,
                resource_revision=None,
                error=_error(
                    "INVALID_CORRECTION",
                    "utterance correction payload is invalid",
                ),
            )
        try:
            value = apply_utterance_correction(
                uow,
                CorrectUtteranceCommand(
                    utterance_id=payload["utterance_id"],
                    expected_revision=operation.base_revision,
                    text=payload["text"],
                    actor="phone-user",
                    speaker_track_id=payload["speaker_track_id"],
                    change_speaker=True,
                    identity=SelfIdentity(payload["identity"]),
                    change_identity=True,
                ),
                self._now(),
            )
        except UtteranceRevisionConflict as exc:
            return OperationReceipt(
                operation_id=operation.operation_id,
                status=ClientOperationStatus.CONFLICT,
                resource_revision=None,
                error=_error("REVISION_CONFLICT", str(exc)),
            )
        except (KeyError, ValueError) as exc:
            return OperationReceipt(
                operation_id=operation.operation_id,
                status=ClientOperationStatus.REJECTED,
                resource_revision=None,
                error=_error("INVALID_CORRECTION", str(exc)),
            )
        return OperationReceipt(
            operation_id=operation.operation_id,
            status=ClientOperationStatus.APPLIED,
            resource_revision=value["revision"],
            error=None,
        )


    def _assign_speaker(self, operation: ClientOperation, uow: UnitOfWork) -> OperationReceipt:
        payload = operation.payload
        try:
            selections = payload.get("selections")
            if not isinstance(selections, list):
                raise AnnotationValidationError("标注缺少发言选择")
            if operation.kind == "segment.classify":
                result = classify_segments(uow, selections, payload.get("sound_kind"),
                    actor=f"phone-operation:{operation.operation_id}", now=self._now())
            else:
                person_id, name = payload.get("person_id"), payload.get("display_name")
                new_person_id = payload.get("new_person_id")
                if any(value is not None and not isinstance(value, str)
                       for value in (person_id, name, new_person_id)):
                    raise AnnotationValidationError("标注人物信息无效")
                if new_person_id is not None:
                    if not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}|[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", new_person_id):
                        raise AnnotationValidationError("本地人物 ID 无效")
                result = assign_annotation(uow, selections, person_id=person_id,
                    display_name=name, new_person_id=new_person_id,
                    actor=f"phone-operation:{operation.operation_id}", now=self._now())
        except UtteranceRevisionConflict as exc:
            return OperationReceipt(operation.operation_id, ClientOperationStatus.CONFLICT,
                                    None, _error("REVISION_CONFLICT", str(exc)))
        except AnnotationValidationError as exc:
            return OperationReceipt(operation.operation_id, ClientOperationStatus.REJECTED,
                                    None, _error("INVALID_ANNOTATION", str(exc)))
        return OperationReceipt(operation.operation_id, ClientOperationStatus.APPLIED,
                                max(row["revision"] for row in result["utterances"]), None,
                                tuple({'resource_id': row['utterance_id'], 'revision': row['revision']} for row in result['utterances']))


class MobileSyncService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        operation_handler: ClientOperationHandler | None = None,
        now: DateTimeClock | None = None,
        annotation_samples: Callable | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._handler = operation_handler or RejectingClientOperationHandler()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._annotation_samples = annotation_samples

    def synchronize(self, device_id: str, request: SyncRequest) -> SyncResponse:
        started = time.perf_counter()
        snapshot = re.fullmatch(r"snapshot-v1-(0|[1-9][0-9]*)-(0|[1-9][0-9]*)", request.cursor or "")
        watermark = int(snapshot[1]) if snapshot else None
        position = int(snapshot[2]) if snapshot else 0
        cursor_sequence = 0 if snapshot else _decode_cursor(request.cursor)
        if snapshot and position > watermark:
            raise ValueError("invalid snapshot cursor")
        receipts: list[OperationReceipt] = []
        with self._uow_factory() as uow:
            transaction_started = time.perf_counter()
            wait_ms = (transaction_started - started) * 1000
            if watermark is not None and watermark > uow.changes.high_water():
                raise ValueError("snapshot cursor exceeds change log")
            uow.people.cache_annotation_people = True
            for operation in request.client_operations:
                receipts.append(self._apply_once(device_id, operation, uow))
            uow.mobile_sync.acknowledge_cursor(device_id, PROJECTION_VERSION, cursor_sequence)
            uow.audit.append("device.sync", f"device:{device_id}", "device", device_id,
                {"cursor": request.cursor, "operation_count": len(request.client_operations)})
        transaction_ms = (time.perf_counter() - transaction_started) * 1000
        # Large first-sync reads and response construction never hold the writer.
        with self._uow_factory().reading() as uow:
            latest = uow.changes.high_water()
            if watermark is None and request.bootstrap and cursor_sequence == 0:
                watermark = latest
            events = (uow.changes.snapshot_after(watermark, position, request.pull_limit + 1)
                      if watermark is not None else uow.changes.list_after(cursor_sequence, request.pull_limit + 1))
        selected = events[:request.pull_limit]
        changes = tuple(
            SyncChange(
                sequence=event.sequence,
                resource_type=event.resource_type,
                resource_id=event.resource_id,
                revision=event.revision,
                operation=event.operation.value,
                resource=(
                    None if event.operation.value == "tombstone" else (
                        validate_utterance_dto(event.payload)
                        if event.resource_type == "utterance" and event.payload is not None
                        else validate_reminder_dto(event.payload)
                        if event.resource_type == "reminder" and event.payload is not None
                        else event.payload
                    )
                ),
            )
            for event in selected
        )
        if request.pull_bytes is not None:
            selected_changes = []
            size = 512 + len(json.dumps([r.as_dict() for r in receipts], ensure_ascii=False).encode("utf-8"))
            for change in changes:
                encoded_size = len(json.dumps(change.as_dict(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1
                # A single oversized resource must still make progress.
                if selected_changes and size + encoded_size > request.pull_bytes:
                    break
                selected_changes.append(change)
                size += encoded_size
            changes = tuple(selected_changes)
        has_more = len(events) > len(changes)
        if watermark is not None:
            next_cursor = (f"snapshot-v1-{watermark}-{changes[-1].sequence}" if has_more
                           else _encode_cursor(watermark))
            has_more = has_more or latest > watermark
        else:
            next_cursor = _encode_cursor(changes[-1].sequence if changes else cursor_sequence)
        if request.client_operations:
            logging.getLogger(__name__).debug("annotation phase=T4-commit ops=%s count=%d service_ms=%.1f wait_ms=%.1f transaction_ms=%.1f",
                ",".join(op.operation_id for op in request.client_operations[:32]), len(request.client_operations),
                (time.perf_counter()-started)*1000, wait_ms, transaction_ms)
        if self._annotation_samples is not None:
            applied = {r.operation_id for r in receipts if r.status is ClientOperationStatus.APPLIED}
            selections = [s for op in request.client_operations
                          if op.kind == "speaker.assign" and op.operation_id in applied
                          for s in op.payload.get("selections", [])]
            if selections:
                self._annotation_samples(selections)
        return SyncResponse(
            projection_version=PROJECTION_VERSION,
            receipts=tuple(receipts),
            changes=changes,
            next_cursor=next_cursor,
            has_more=has_more,
            server_time=self._now().astimezone(timezone.utc),
            transaction_ms=round(transaction_ms, 3) if request.pull_bytes is not None else None,
        )

    def _apply_once(
        self,
        device_id: str,
        operation: ClientOperation,
        uow: UnitOfWork,
    ) -> OperationReceipt:
        digest = _operation_sha256(operation)
        existing = uow.mobile_sync.find_operation(operation.operation_id)
        if existing is not None:
            if existing.device_id == device_id and existing.payload_sha256 == digest:
                if (existing.receipt.status is ClientOperationStatus.APPLIED
                    and existing.receipt.resource_results is None
                    and operation.kind in {'speaker.assign', 'segment.classify'}):
                    return uow.mobile_sync.recover_annotation_receipt(existing)
                return existing.receipt
            return OperationReceipt(
                operation_id=operation.operation_id,
                status=ClientOperationStatus.CONFLICT,
                resource_revision=None,
                error=_error(
                    "OPERATION_ID_REUSED",
                    "operation_id was already used with different content",
                ),
            )
        effective = _merge_annotation_revision(device_id, operation, uow)
        receipt = self._handler.apply(effective, uow)
        if (
            receipt.operation_id != operation.operation_id
            or receipt.status is ClientOperationStatus.PENDING
        ):
            raise ValueError("client operation handler returned an invalid receipt")
        if not uow.mobile_sync.record_operation(
            device_id,
            operation,
            digest,
            receipt,
        ):
            raise RuntimeError("client operation raced with another transaction")
        return receipt


def _merge_annotation_revision(device_id, operation, uow):
    """Rebase only a complete correction history; never mutate the wire payload.

    Different fields commute. Same-field predecessors must be explicitly named,
    already applied and owned by this device. Text/evidence replacement never
    commutes with a speaker or sound decision.
    """
    if operation.kind not in {"speaker.assign", "segment.classify"}:
        return operation
    selections = operation.payload.get("selections")
    if not isinstance(selections, list):
        return operation
    dependencies = operation.payload.get("depends_on", [])
    if not isinstance(dependencies, list) or any(not isinstance(v, str) for v in dependencies):
        return operation
    allowed_actors = set()
    for dependency in dependencies:
        record = uow.mobile_sync.find_operation(dependency)
        if record and record.device_id == device_id and record.receipt.status is ClientOperationStatus.APPLIED:
            allowed_actors.add(f"phone-operation:{dependency}")
    other_fields = ({"sound_kind"} if operation.kind == "speaker.assign"
                    else {"speaker_track_id", "identity", "person_annotation"})
    annotation_fields = {"sound_kind", "speaker_track_id", "identity", "person_annotation"}
    rebased = []
    for selection in selections:
        if not isinstance(selection, dict) or type(selection.get("revision")) is not int:
            return operation
        try:
            current = uow.evidence.get_utterance(selection.get("utterance_id"))
        except KeyError:
            return operation
        revision = selection["revision"]
        if not 1 <= revision <= current.revision:
            rebased.append(dict(selection))
            continue
        history = [c for c in uow.corrections.list_for_target("utterance", current.utterance_id)
                   if revision <= c.before_revision < current.revision]
        complete = len(history) == current.revision - revision and (
            {c.before_revision for c in history} == set(range(revision, current.revision)))
        safe = revision > 0 and revision <= current.revision and complete and all(
            (set(c.patch) - {"previous_values"}) <= other_fields or
            (c.actor in allowed_actors and (set(c.patch) - {"previous_values"}) <= annotation_fields)
            for c in history)
        rebased.append({**selection, "revision": current.revision} if safe else dict(selection))
    return replace(operation, payload={**operation.payload, "selections": rebased})


def _operation_sha256(operation: ClientOperation) -> str:
    payload = json.dumps(
        {
            "operation_id": operation.operation_id,
            "kind": operation.kind,
            "base_revision": operation.base_revision,
            "payload": operation.payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _decode_cursor(value: str | None) -> int:
    if value is None:
        return 0
    match = _CURSOR.fullmatch(value)
    if match is None:
        raise ValueError("invalid sync cursor")
    return int(match.group(1))


def _encode_cursor(sequence: int) -> str:
    return f"cursor-{sequence}"


def _error(code: str, message: str) -> dict[str, object]:
    return {
        "code": code,
        "message": message,
        "details": {},
        "request_id": new_ulid(),
    }


__all__ = [
    "ClientOperationHandler",
    "MobileSyncService",
    "RejectingClientOperationHandler",
    "UtteranceCorrectionOperationHandler",
]
