"""Context boundaries from reply edges, shared wording, time and explicit pivots."""

import re
from .chat_projection import semantic_text
from .memory_content import readable
from .recall_language import shared_anchor, object_ids, PIVOT

RULE_VERSION = 3


def split(records):
    groups = []
    for r in records:
        text = semantic_text(r.get("text") or "")
        current = groups[-1] if groups else None
        related = False
        if current:
            prior = current["records"][-1]
            nearby = (r.get("sent_at") or 0) - (prior.get("sent_at") or 0) <= 1800
            reply = r.get("reply") or {}
            explicit = isinstance(reply, dict) and reply.get("record_id") in {
                x["record_id"] for x in current["records"]
            }
            previous = "\n".join(
                semantic_text(x.get("text") or "") for x in current["records"][-4:]
            )
            anchor = shared_anchor(previous, text)
            different = bool(
                object_ids(previous)
                and object_ids(text)
                and not object_ids(previous) & object_ids(text)
            )
            related = (
                nearby
                and not PIVOT.search(text)
                and not different
                and (
                    explicit
                    or bool(anchor)
                    or (
                        0 < len(semantic_text(prior.get("text") or "")) <= 5
                        and text.startswith(semantic_text(prior.get("text") or ""))
                    )
                    or bool(re.match(r"^(?:这|那|它|其|上述|前述)", text))
                    or len(text.strip()) <= 5
                    or not readable(text)
                )
            )
        if not related:
            current = {
                "records": [],
                "object": None,
                "boundary_basis": "bounded_context_excerpt",
            }
            groups.append(current)
        elif explicit:
            current["boundary_basis"] = "explicit_reply_edge"
        elif anchor:
            current["boundary_basis"] = "shared_original_wording_candidate"
        current["records"].append(r)
    return groups


def role(record):
    text = semantic_text(record.get("text") or "")
    return "core" if readable(text) and not text.startswith("http") else "context"
