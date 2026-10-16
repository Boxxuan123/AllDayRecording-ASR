"""Bounded native-label candidate index. Every result retains its source namespace.

This indexes directory metadata only; similarities are query leads, never identity writes.
"""

from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import re

_CACHE = OrderedDict()


def index(directory):
    encoded = json.dumps(directory, ensure_ascii=False, sort_keys=True).encode()
    key = hashlib.sha256(encoded).hexdigest()
    if key not in _CACHE:
        _CACHE[key] = tuple(
            (
                c,
                tuple(
                    str(n).strip().casefold()
                    for n in c.get("names", [])
                    if str(n).strip()
                ),
            )
            for c in directory[:5000]
        )
        if len(_CACHE) > 4:
            _CACHE.popitem(last=False)
    return key, _CACHE[key]


def candidates(directory, mention, source="all"):
    needle = mention.strip().casefold()
    if len(needle) < 2:
        return []
    _, entries = index(directory)
    result = []
    for c, names in entries:
        if source not in {"all", c["platform"]}:
            continue
        exact = any(needle == n for n in names)
        contained = any(needle in n for n in names)
        if not (exact or contained):
            continue
        # Latin short names must be complete identifiers, never embedded letters.
        if re.fullmatch(r"[a-z0-9_]+", needle) and not any(
            re.search(r"(?<![a-z0-9_])" + re.escape(needle) + r"(?![a-z0-9_])", n)
            for n in names
        ):
            continue
        rank = (
            not exact,
            c["conversation_type"] != "c2c",
            c["platform"],
            c["source_account_id"],
            c["key"],
        )
        item = deepcopy(c)
        item["candidate_basis"] = (
            "native_exact_label" if exact else "native_label_contains_mention"
        )
        item["candidate_identity_status"] = "independent_source_lead_not_person_merge"
        result.append((rank, item))
    return [c for _, c in sorted(result, key=lambda x: x[0])]


def subject(question):
    # A grammatical subject is used only if an actual source label supports it.
    text = re.sub(r"^\s*(?:昨天晚上|前天晚上|昨晚|昨天|前天|今天)\s*", "", question)
    text = re.sub(r"^(?:微信|QQ|录音)(?:中|里)?\s*", "", text, flags=re.I)
    match = re.match(r"(.{2,24}?)(?=打算|准备|计划|想|约|最后|为什么|是否|的)", text)
    return match.group(1).strip(" ，。？?") if match else ""


def diverse(values, limit=4):
    # Spend an initial slot in each namespace before revisiting a prolific source.
    groups = {}
    for c in values:
        groups.setdefault((c["platform"], c["source_account_id"]), []).append(c)
    out = []
    while groups and len(out) < limit:
        for key in list(groups):
            out.append(groups[key].pop(0))
            if not groups[key]:
                del groups[key]
            if len(out) == limit:
                break
    return out
