"""Chat-specific evidence projection of the canonical Event, no recording facade."""

import json
from .reminder_support import _datetime


def projection(current, source=None):
    chat = current.payload["chat_followup"]
    pending = (
        json.loads(source["conflict_json"])
        if source and source.get("conflict_json")
        else None
    )

    def evidence(v):
        return [
            {
                "source_type": "chat_record",
                "record_id": r["record_id"],
                "record_revision": r["record_revision"],
                "platform": r["platform"],
                "sent_at": r.get("sent_at"),
                "quote": r["quote"],
                "is_from_self": r.get("is_from_self"),
                "speaker": r.get("sender_display_name")
                or ("我本人" if r.get("is_from_self") is True else "未知昵称"),
                "context": {
                    k: r.get(k)
                    for k in ("reply", "forward", "source_quote")
                    if r.get(k)
                },
            }
            for r in v.get("evidence", [])
        ]

    return {
        "event_id": current.event_id,
        "revision": current.revision,
        "source_key": chat["source_key"],
        "title": current.payload.get("title", ""),
        "status": current.status.value,
        "category": chat.get("category", "pending"),
        "actor_is_from_self": chat.get("actor_is_from_self"),
        "time": chat.get("time", {}),
        "historical": bool(chat.get("historical")),
        "progress_unknown": bool(chat.get("progress_unknown")),
        "user_confirmed": bool(chat.get("user_confirmed")),
        "ignored": bool(source and source.get("ignored")),
        "uncertainty": chat.get("uncertainty", []),
        "evidence": evidence(chat),
        "pending_change": {
            "action": pending["action"],
            "kind": pending.get("change_kind", "progress"),
            "time": pending["time"],
            "uncertainty": pending.get("uncertainty", []),
            "evidence": evidence(pending),
        }
        if pending
        else None,
        "updated_at": _datetime(current.updated_at),
    }


def publish(uow, current, source=None):
    uow.changes.append(
        "chat_followup",
        current.event_id,
        current.revision,
        "upsert",
        projection(current, source),
    )
