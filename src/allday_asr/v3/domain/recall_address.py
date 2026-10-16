"""Query-local direct addresses grounded in bilateral original private messages."""

import re


def direct_address(rows, mention, directory):
    if not mention or len(mention) > 100:
        return None
    candidates = {}
    for row in rows:
        if (
            row.get("conversation_type") != "c2c"
            or row.get("source_is_from_self") is not True
        ):
            continue
        text = row.get("quote", "")
        # A quoted name, a third-person mention or an incoming address is not the peer.
        if not re.match(
            r"^\s*" + re.escape(mention) + r"\s*[，,:：!！]\s*(?:你|您)", text
        ):
            continue
        key = tuple(
            row.get(k) for k in ("source", "source_account_id", "conversation_id")
        )
        replies = [
            r
            for r in rows
            if tuple(
                r.get(k) for k in ("source", "source_account_id", "conversation_id")
            )
            == key
            and r.get("source_is_from_self") is False
            and r.get("sender_account_id")
            and 0 < (r.get("sent_at") or 0) - row["sent_at"] <= 21600
        ]
        if not replies or any(
            re.search(
                r"我不是|不是我|你(?:说|叫)的.{0,12}(?:是谁|哪位)|叫错|认错", r["quote"]
            )
            for r in replies
        ):
            continue
        peers = {r["sender_account_id"] for r in replies}
        contacts = [
            c
            for c in directory
            if (c["platform"], c["source_account_id"], c["conversation_id"]) == key
            and c["conversation_type"] == "c2c"
        ]
        if len(peers) == len(contacts) == 1:
            candidates[key] = (
                {**contacts[0], "sender_account_id": next(iter(peers))},
                [row, *replies],
            )
    return next(iter(candidates.values())) if len(candidates) == 1 else None
