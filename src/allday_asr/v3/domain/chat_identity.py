"""Live tri-state metadata; frozen message revisions and manual links stay separate."""

from .chat_data import packed
from allday_asr.v3.ports.chat_data import ChatDataError

IDENTITY_FIELDS = frozenset({"is_from_self", "self_identity_basis"})


def frozen_record(record):
    def strip(value, root=False):
        if isinstance(value, dict):
            is_record = root or {"record_id", "record_revision"} <= value.keys()
            return {
                k: strip(v)
                for k, v in value.items()
                if not (is_record and k in IDENTITY_FIELDS)
            }
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    return strip(record, True)


def merge_live_identity(previous, incoming):
    result = dict(previous)
    if "is_from_self" in incoming:
        result.update(checked_identity(incoming))

    def merge(old, new):
        if isinstance(old, dict) and isinstance(new, dict):
            is_record = {"record_id", "record_revision"} <= old.keys()
            result = {k: merge(v, new[k]) if k in new else v for k, v in old.items()}
            if is_record and "is_from_self" in new:
                result.update(checked_identity(new))
            return result
        if isinstance(old, list) and isinstance(new, list):
            return [merge(a, b) for a, b in zip(old, new, strict=True)]
        return old

    for key in previous:
        if key in incoming and key not in IDENTITY_FIELDS:
            result[key] = merge(previous[key], incoming[key])
    return result


def checked_identity(record):
    if not isinstance(record, dict):
        raise ChatDataError("INVALID_CHAT_RECORD", 502)
    flag = record.get("is_from_self")
    basis = record.get("self_identity_basis", "unknown")
    if (
        (flag is not None and type(flag) is not bool)
        or not isinstance(basis, str)
        or not basis
        or len(basis) > 200
    ):
        raise ChatDataError("INVALID_SELF_IDENTITY", 502)
    return {"is_from_self": flag, "self_identity_basis": basis}


def link_person(value):
    return value.get("person_id") if isinstance(value, dict) else value


def message_self(record, links):
    flag = checked_identity(record)["is_from_self"]
    if flag is not None:
        return flag
    account = record.get("sender_account_id")
    value = (
        links.get(packed([record["platform"], record["source_account_id"], account]))
        if account
        else None
    )
    # Old strings are existing explicit manual confirmations. Auto mappings never turn null into false/true.
    if isinstance(value, str):
        return value == "self"
    if isinstance(value, dict) and value.get("origin") == "manual":
        return value["person_id"] == "self"
    return None


def actor_identity(anchor, actor, evidence, links):
    matches = [r for r in evidence if r.get("sender_account_id") == actor]
    if not actor:
        matches = [anchor] if not anchor.get("sender_account_id") else []
    states = {message_self(r, links) for r in matches} - {None}
    flag = next(iter(states)) if len(states) == 1 else None
    witnessed = bool(matches) and (bool(actor) or flag is True)
    key = packed([anchor["platform"], anchor["source_account_id"], actor])
    person = (
        "self" if flag is True else link_person(links.get(key)) if witnessed else None
    )
    if flag is not True and person == "self":
        person = None
    return witnessed, person, flag, len(states) > 1
