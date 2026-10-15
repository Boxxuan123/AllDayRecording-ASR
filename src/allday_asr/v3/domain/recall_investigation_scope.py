"""Intersect remote grants with requested source constraints; never widen on fallback."""

KEYS = ("platform", "source_account_id", "conversation_id", "sender_account_id")


def _intersect(left, right):
    if any(left.get(k) and right.get(k) and left[k] != right[k] for k in KEYS):
        return None
    return {k: right.get(k) or left.get(k) for k in KEYS if right.get(k) or left.get(k)}


def remote_grants(scope, grants, directory):
    # A person without a resolved grant must not fall back to account-wide reads.
    if not grants and not scope.get("person_id"):
        grants = [{k: c[k] for k in KEYS[:2]} for c in directory]
    grants = [dict(g) for g in grants if scope["source"] in {"all", g.get("platform")}]
    constraints = [
        (scope.get("source_accounts"), KEYS[:2]),
        (scope.get("source_contacts"), KEYS[:3]),
        ([{"sender_account_id": s} for s in scope.get("source_senders", [])], KEYS),
    ]
    for choices, keys in constraints:
        if not choices:
            continue
        # Contacts constrain conversations, not direction: keep both c2c speakers.
        filters = [{k: c[k] for k in keys if c.get(k)} for c in choices]
        grants = [
            merged
            for g in grants
            for c in filters
            if (merged := _intersect(g, c)) is not None
        ]
    result = []
    for grant in grants:
        grant = {k: grant[k] for k in KEYS if grant.get(k)}
        if all(grant.get(k) for k in KEYS[:2]) and grant not in result:
            result.append(grant)
    return result


def contact_record_allowed(record, scope):
    contacts = scope.get("source_contacts")
    if not contacts:
        return (
            scope.get("relationship") != "group"
            or record.get("conversation_type") == "group"
        )
    for contact in contacts:
        if any(record.get(k) != contact.get(k) for k in KEYS[:3]):
            continue
        if record.get("conversation_type") != contact.get("conversation_type"):
            continue
        if (
            contact["conversation_type"] == "group"
            and scope.get("relationship") != "speaker"
        ):
            return True
        sender = contact.get("sender_account_id")
        if record.get("is_from_self") is True:
            return scope.get("relationship") != "speaker"
        if record.get("is_from_self") is False and (
            not sender or record.get("sender_account_id") == sender
        ):
            return True
    return False
