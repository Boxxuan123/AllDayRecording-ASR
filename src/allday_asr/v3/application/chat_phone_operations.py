"""Phone commands use the same Event operations in the existing sync transaction."""

from contextlib import nullcontext
from .chat_followups import ChatFollowups
from .reminders import IntelligentReminderService
from allday_asr.v3.ports.chat_data import ChatDataError
from allday_asr.v3.domain.device_sync import OperationReceipt, ClientOperationStatus


def apply_chat_operation(operation, uow, now):
    p = operation.payload
    try:
        if (
            set(p) - {"event_id", "action", "changes"}
            or not {"event_id", "action"} <= set(p)
            or operation.base_revision is None
        ):
            raise ChatDataError("INVALID_CHAT_COMMAND", 422)
        sources = uow.followups.for_event(p["event_id"])
        if len(sources) != 1:
            raise ChatDataError("CHAT_SOURCE_NOT_UNIQUE", 409)
        if p["action"] not in {
            "confirm",
            "edit",
            "complete",
            "cancel",
            "ignore",
            "reopen",
            "accept_change",
            "dismiss_change",
            "enable_reminder",
        }:
            raise ChatDataError("INVALID_FOLLOWUP_ACTION", 422)
        service = ChatFollowups(
            lambda: nullcontext(uow),
            now=lambda: now,
            reminders=IntelligentReminderService(
                lambda: nullcontext(uow), None, now=lambda: now
            ),
        )
        with uow.savepoint():
            result = service.act(
                sources[0]["source_key"],
                p["action"],
                p.get("changes", {}),
                operation.base_revision,
                actor="phone-user:" + operation.operation_id,
            )
    except (ChatDataError, ValueError, KeyError, TypeError) as exc:
        code = exc.code if isinstance(exc, ChatDataError) else "INVALID_CHAT_COMMAND"
        return OperationReceipt(
            operation.operation_id,
            ClientOperationStatus.CONFLICT
            if isinstance(exc, ChatDataError) and exc.status == 409
            else ClientOperationStatus.REJECTED,
            None,
            {
                "code": code,
                "message": "事项已变化，请同步核对；原操作和状态保留",
                "retryable": False,
            },
        )
    return OperationReceipt(
        operation.operation_id,
        ClientOperationStatus.APPLIED,
        result["revision"],
        None,
        ({"resource_id": result["event_id"], "revision": result["revision"]},),
    )
