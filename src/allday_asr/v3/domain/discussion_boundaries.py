"""Conservative segmentation: source reply edges or explicit object references."""

from .chat_projection import semantic_text
from .memory_content import readable
from .recall_language import common_object_id, object_ids, PIVOT

RULE_VERSION = 4


def split(records):
    groups = []
    for r in records:
        text = semantic_text(r.get("text") or "")
        current = groups[-1] if groups else None
        basis = "bounded_context_excerpt"
        related = False
        if current:
            prior = current["records"][-1]
            delta = (r.get("sent_at") or 0) - (prior.get("sent_at") or 0)
            namespace = ("dataset", "platform", "source_account_id", "conversation_id")
            same_conversation = all(prior.get(k) == r.get(k) for k in namespace)
            reply = r.get("reply") or {}
            explicit = isinstance(reply, dict) and reply.get("record_id") in {
                x["record_id"] for x in current["records"]
            }
            previous = semantic_text(prior.get("text") or "")
            object_id = common_object_id(previous, text)
            left, right = object_ids(previous), object_ids(text)
            different = bool(left and right and not left & right)
            # Nearby/common words, short sentences and pronouns are insufficient.
            # Opaque media may accompany a segment only as context, never its facts.
            related = (
                same_conversation
                and 0 <= delta <= 1800
                and not PIVOT.search(text)
                and not different
                and (explicit or bool(object_id) or not readable(text))
            )
            if explicit:
                basis = "explicit_reply_edge"
            elif object_id:
                basis = "explicit_object_reference"
        if not related:
            current = {
                "records": [],
                "object": None,
                "boundary_basis": "bounded_context_excerpt",
            }
            groups.append(current)
        elif basis != "bounded_context_excerpt":
            current["boundary_basis"] = basis
        current["records"].append(r)
    return groups


def role(record):
    text = semantic_text(record.get("text") or "")
    return "core" if readable(text) and not text.startswith("http") else "context"
