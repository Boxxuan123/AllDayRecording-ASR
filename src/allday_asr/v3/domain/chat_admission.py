"""Conservative, evidence-scoped personal admission before Event creation."""

from hashlib import sha256
import re
from .chat_data import packed
from .chat_followups import actor_key
from .chat_projection import semantic_text

POLICY = "chat-personal-admission-v2"


def evidence_digest(value):
    return sha256(
        packed(
            sorted(
                [
                    [
                        r["evidence_key"],
                        r.get("raw_text_sha256")
                        or sha256((r.get("text") or "").encode()).hexdigest(),
                        r.get("sender_account_id"),
                        r.get("sent_at"),
                    ]
                    for r in value["evidence"]
                ]
            )
        ).encode()
    ).hexdigest()


def decide(value, links, saved=None):
    digest = evidence_digest(value)
    if saved and saved["origin"] != "rule":
        if (
            saved["evidence_digest"] != digest
            or saved["conversation_key"] != value["conversation_key"]
        ):
            return "text_only", "review_evidence_changed"
        if saved["decision"] in {"text_only", "conditional"}:
            return saved["decision"], saved["origin"]
        if saved["origin"] == "human_selection":
            # A cared-person selection is issue-specific and still needs a witnessed person link.
            if (
                saved["decision"] == "cared"
                and value["actor_person_id"] in links.values()
                and value["actor_person_id"] != "self"
            ):
                return "cared", "human_selection"
            if saved["decision"] == "cared":
                return "text_only", "person_relation_not_established"
            if saved["decision"] == "task":
                return "task", "human_selection"
    # A sender mapping alone is insufficient. Only narrowly witnessed responsibility
    # is admitted by these local rules; no claim of complete semantic recall.
    if value["commitment"] != "explicit" or value["relationship"] != "explicit":
        return "text_only", "commitment_not_established"
    actor = value.get("actor_account_id")
    for r in value["evidence"]:
        quote = r["quote"]
        if re.search(
            r"如果|假如|要是|有空|可能|考虑|打算|他说|她说|据说|听说|转述|不(?:会|再|负责|答应|承诺|处理)|没(?:答应|承诺)",
            quote,
        ):
            continue
        if r.get("sender_account_id") != actor or semantic_text(
            r.get("text", "")
        ) != r.get("text", ""):
            continue
        # Do not attribute a quoted author's promise to the outer sender.
        forwarded = r.get("forward")
        has_forward = (
            any(v for k, v in forwarded.items() if k not in {"resolution", "status"})
            if isinstance(forwarded, dict)
            else bool(forwarded)
        )
        if (
            "[引用开始]" in r.get("text", "")
            or r.get("text", "").startswith(("转发：", "引用："))
            or has_forward
            or r.get("source_quote")
        ):
            continue
        if links.get(actor_key(r, actor)) == "self" and re.search(
            r"我(?:来负责|负责|答应|承诺|会给你|来处理|给你处理)|交给我", quote
        ):
            return "task", "witnessed_self_responsibility"
        reply = r.get("reply")
        target_id = reply.get("record_id") if isinstance(reply, dict) else None
        recipient = next(
            (e for e in value["evidence"] if e["record_id"] == target_id), None
        )
        if (
            recipient
            and links.get(actor_key(recipient, recipient.get("sender_account_id")))
            == "self"
            and recipient.get("sender_account_id")
            in value.get("recipient_account_ids", [])
            and re.search(
                r"我(?:会给你|给你|来负责|负责|答应|承诺|来处理)|交给我", quote
            )
        ):
            return "task", "witnessed_promise_to_self"
    return "text_only", "personal_relevance_not_established"
