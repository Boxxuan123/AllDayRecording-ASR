"""Intersect remote grants with requested source constraints; never widen on fallback."""

KEYS = ("platform", "source_account_id", "conversation_id", "sender_account_id")


def platform_allowed(scope, platform):
    return (scope.get("source", "all") in {"all", platform}
            and (not scope.get("source_platforms") or platform in scope["source_platforms"]))


def _intersect(left, right):
    if any(left.get(k) and right.get(k) and left[k] != right[k] for k in KEYS):
        return None
    return {k: right.get(k) or left.get(k) for k in KEYS if right.get(k) or left.get(k)}


def remote_grants(scope, grants, directory):
    # A person without a resolved grant must not fall back to account-wide reads.
    if not grants and not scope.get("person_id"):
        grants = [{k: c[k] for k in KEYS[:2]} for c in directory]
    grants = [dict(g) for g in grants if platform_allowed(scope, g.get("platform"))]
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
    allowed = set(root.get("source_platforms", ["qq", "wechat", "recording"])) & set(
        scope.get("source_platforms", ["qq", "wechat", "recording"])
    )
    if not allowed or (
        scope.get("source", "all") != "all" and scope["source"] not in allowed
    ):
        return None
    if root.get("source_platforms") or scope.get("source_platforms"):
        scope["source_platforms"] = sorted(allowed)
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
        choices = [c for c in choices if platform_allowed(scope, c["platform"])]
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
    if root.get("source_windows"):
        # Root pairs are immutable; a dynamic sub-window may only narrow them.
        requested = scope.get("source_windows", root["source_windows"])
        windows = []
        for hard in root["source_windows"]:
            if not platform_allowed(scope, hard["platform"]):
                continue
            if scope.get("source_contacts") and not any(
                all(c.get(k) == hard[k] for k in KEYS[:3])
                for c in scope["source_contacts"]
            ):
                continue
            for choice in requested:
                if any(choice.get(k) != hard[k] for k in KEYS[:3]):
                    continue
                a = max(hard["sent_from"], choice["sent_from"], scope["sent_from"])
                b = min(hard["sent_to"], choice["sent_to"], scope["sent_to"])
                if a <= b:
                    windows.append({**hard, "sent_from": a, "sent_to": b})
        if not windows:
            return None
        scope["source_windows"] = windows
        scope["sent_from"] = min(w["sent_from"] for w in windows)
        scope["sent_to"] = max(w["sent_to"] for w in windows)
    return scope


def inside_root(value, root, *, evidence=False):
    from .recall_scope_windows import window_allows

    if not window_allows(value, root, evidence=evidence):
        return False
    platform = value.get("platform", value.get("source"))
    if not platform_allowed(root, platform):
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
