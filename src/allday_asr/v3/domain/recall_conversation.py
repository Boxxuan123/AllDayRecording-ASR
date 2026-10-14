"""Pure bilateral participation checks; quoted authors never identify senders."""

from .source_identity import resolve_chat, effective_self


def source_target(record, contacts):
    for contact in contacts:
        if any(
            record.get(k) != contact[k]
            for k in ("platform", "source_account_id", "conversation_id")
        ):
            continue
        if record.get("conversation_type") != contact["conversation_type"]:
            continue
        sender = contact.get("sender_account_id")
        if sender:
            return (
                record.get("sender_account_id") == sender
                and record.get("is_from_self") is not True
            )
        return (
            contact["conversation_type"] == "c2c"
            and record.get("is_from_self") is False
        )
    return False


def participants(records, people, manual, scope):
    """Keep bilateral private messages, or verified bilateral group reply pairs."""
    private, groups = {}, {}
    for record in records:
        person, _ = resolve_chat(record, manual, people)
        is_self = (
            (record.get("is_from_self") is True)
            if scope.get("source_contacts")
            else effective_self(record, manual, people) is True
        )
        target = (
            source_target(record, scope["source_contacts"])
            if scope.get("source_contacts")
            else bool(scope["person_id"]) and person == scope["person_id"]
        )
        if not target and not is_self:
            continue
        key = tuple(
            record[k] for k in ("platform", "source_account_id", "conversation_id")
        )
        entry = (record, target, is_self)
        if record.get("conversation_type") == "c2c":
            private.setdefault(key, []).append(entry)
        elif record.get("conversation_type") == "group":
            groups.setdefault(key, []).append(entry)
    selected = []
    for entries in private.values():
        # A person's monologue does not prove a conversation with the user.
        if scope.get("source_contacts") or (
            any(e[1] for e in entries) and any(e[2] for e in entries)
        ):
            selected.extend(e[0] for e in entries)
    for entries in groups.values():
        by_id = {r["record_id"]: (r, target, self_) for r, target, self_ in entries}
        paired = set()
        for r, target, self_ in entries:
            reply = r.get("reply") or {}
            other = by_id.get(reply.get("record_id"))
            if other and ((target and other[2]) or (self_ and other[1])):
                paired.update((r["record_id"], other[0]["record_id"]))
        selected.extend(by_id[rid][0] for rid in paired)
    return selected


def native_present(row, scope):
    if row["source"] == "recording" or not scope.get("source_contacts"):
        return row
    contact = next(
        (
            c
            for c in scope["source_contacts"]
            if c["platform"] == row["source"]
            and c["source_account_id"] == row["source_account_id"]
            and c["conversation_id"] == row["conversation_id"]
        ),
        None,
    )
    if not contact or contact["conversation_type"] != "c2c":
        return row
    row = dict(row)
    raw = row.get("source_is_from_self")
    row["effective_is_self"] = raw
    row["is_from_self"] = raw
    row["person_id"] = ""
    row["speaker"] = (
        "我"
        if raw is True
        else contact["display_name"]
        if raw is False
        else "未确定发送方向"
    )
    row["identity_resolution"] = "source_native"
    row["identity_basis"] = "原生私聊会话与消息方向；本次查询不确认现实人物关系"
    return row
