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


def intersect_root(root, dynamic):
    """Intersect immutable user filters; None means an empty, unqueryable scope."""
    from copy import deepcopy

    scope = deepcopy(dynamic)
    for key, wildcard in (("source", "all"), ("person_id", "")):
        hard = root.get(key, wildcard)
        current = scope.get(key, wildcard)
        if hard != wildcard and current not in (wildcard, hard):
            return None
        if hard != wildcard:
            scope[key] = hard
    scope["sent_from"] = max(scope["sent_from"], root["sent_from"])
    scope["sent_to"] = min(scope["sent_to"], root["sent_to"])
    if scope["sent_from"] > scope["sent_to"]:
        return None
    for field, keys in (("source_accounts", KEYS[:2]), ("source_contacts", KEYS[:3])):
        hard = root.get(field)
        if not hard:
            continue
        current = scope.get(field)
        choices = []
        for wanted in hard:
            for candidate in current or [wanted]:
                if any(wanted.get(k) != candidate.get(k) for k in keys):
                    continue
                if field == "source_contacts" and any(
                    wanted.get(k) and candidate.get(k) and wanted[k] != candidate[k]
                    for k in ("conversation_type", "sender_account_id")
                ):
                    continue
                choices.append({**candidate, **wanted})
        choices = [c for c in choices if scope["source"] in {"all", c["platform"]}]
        if not choices:
            return None
        scope[field] = choices
    if scope.get("source_contacts") and scope.get("source_accounts"):
        scope["source_contacts"] = [
            c
            for c in scope["source_contacts"]
            if any(
                all(c[k] == a[k] for k in KEYS[:2]) for a in scope["source_accounts"]
            )
        ]
        if not scope["source_contacts"]:
            return None
    return scope


def inside_root(value, root, *, evidence=False):
    platform = value.get("platform", value.get("source"))
    if root.get("source", "all") not in {"all", platform}:
        return False
    for field, keys in (("source_accounts", KEYS[:2]), ("source_contacts", KEYS[:3])):
        if root.get(field) and not any(
            all(
                (platform if k == "platform" else value.get(k)) == c.get(k)
                for k in keys
            )
            for c in root[field]
        ):
            return False
    if evidence:
        if not root["sent_from"] <= (value.get("sent_at") or 0) <= root["sent_to"]:
            return False
        if root.get("person_id") and value.get("person_id") != root["person_id"]:
            return False
        if root.get("source_contacts"):
            record = {
                **value,
                "platform": platform,
                "is_from_self": value.get(
                    "source_is_from_self", value.get("is_from_self")
                ),
            }
            return contact_record_allowed(record, root)
    return True
