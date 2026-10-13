"""Stable source keys and conservative, read-time person resolution."""

from allday_asr.v3.domain.chat_data import packed


def identity_key(record):
    account = record.get("sender_account_id")
    return (
        packed([record["platform"], record["source_account_id"], account])
        if account
        else None
    )


def resolve_chat(record, manual, people):
    key = identity_key(record)
    self_id = next((p for p, v in people.items() if v["kind"] == "self"), None)
    if key in manual:
        person = manual[key]
        person = self_id if person == "self" else person
        return (person if person in people else None), "人工账号映射（含解除／纠正）"
    if record.get("is_from_self") is True:
        return self_id, "Mac 本人标记：" + record.get(
            "self_identity_basis", "is_from_self"
        )
    return None, "稳定来源账号；尚未关联人物"
