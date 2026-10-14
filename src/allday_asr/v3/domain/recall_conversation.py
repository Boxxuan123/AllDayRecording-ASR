"""Pure bilateral participation checks; quoted authors never identify senders."""

from .source_identity import resolve_chat, effective_self


def participants(records, people, manual, scope):
    """Keep bilateral private messages, or verified bilateral group reply pairs."""
    private, groups = {}, {}
    for record in records:
        person, _ = resolve_chat(record, manual, people)
        is_self = effective_self(record, manual, people) is True
        if person != scope["person_id"] and not is_self:
            continue
        key = tuple(
            record[k] for k in ("platform", "source_account_id", "conversation_id")
        )
        entry = (record, person == scope["person_id"], is_self)
        if record.get("conversation_type") == "c2c":
            private.setdefault(key, []).append(entry)
        elif record.get("conversation_type") == "group":
            groups.setdefault(key, []).append(entry)
    selected = []
    for entries in private.values():
        # A person's monologue does not prove a conversation with the user.
        if any(e[1] for e in entries) and any(e[2] for e in entries):
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
