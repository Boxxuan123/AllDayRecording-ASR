"""Joint native namespace/date predicates; never replace them with a bounding range."""

from copy import deepcopy

KEYS = ("platform", "source_account_id", "conversation_id")


def window_allows(value, scope, *, evidence=False):
    windows = scope.get("source_windows")
    if not windows:
        return True
    if evidence and type(value.get("sent_at")) is not int:
        return False
    platform = value.get("platform", value.get("source"))
    for w in windows:
        if any(value.get(k) and value[k] != w[k] for k in KEYS if k != "platform"):
            continue
        if platform and platform != w["platform"]:
            continue
        if (
            value.get("conversation_type")
            and value["conversation_type"] != w["conversation_type"]
        ):
            continue
        if evidence and not w["sent_from"] <= value.get("sent_at", -1) <= w["sent_to"]:
            continue
        return True
    return False


def scope_parts(scope):
    if not scope.get("source_windows"):
        return [scope]
    from .recall_investigation_scope import intersect_root

    parts = []
    for w in scope["source_windows"]:
        s = deepcopy(scope)
        s["source_windows"] = [deepcopy(w)]
        root = {
            "source": w["platform"],
            "sent_from": w["sent_from"],
            "sent_to": w["sent_to"],
            "source_contacts": [dict(w)],
            "source_windows": [deepcopy(w)],
        }
        s = intersect_root(root, s)
        if s is not None:
            parts.append(s)
    return parts


def record_scope(scope, row):
    return next(
        (s for s in scope_parts(scope) if window_allows(row, s, evidence=True)), None
    )
