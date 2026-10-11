"""Bounded, frozen original evidence for existing items across analysis batches."""

from hashlib import sha256
from allday_asr.v3.domain.chat_followups import conversation_key, evidence_key
from allday_asr.v3.domain.chat_data import packed
from allday_asr.v3.ports.chat_data import ChatDataError


def augment(controller, records, value):
    conv = conversation_key(records[0]) if records else None
    merged = {r["record_id"]: r for r in records}
    existing, unavailable = [], []
    offset = 0
    while len(existing) < 15:
        page = controller.service.list(100, offset)
        for item in page["items"]:
            if item["conversation_key"] != conv or item["dataset"] != value["dataset"]:
                continue
            chat = item["chat"]
            old = []
            try:
                for proof in chat.get("evidence", []):
                    r = controller.chat.cache.record(proof["record_id"])
                    if r is None:
                        r = next(
                            (
                                r
                                for r in value["records"]
                                if r["record_id"] == proof["record_id"]
                            ),
                            None,
                        )
                    if (
                        not r
                        or conversation_key(r) != conv
                        or evidence_key(value["dataset"], r) != proof["evidence_key"]
                        or sha256((r.get("text") or "").encode()).hexdigest()
                        != proof["raw_text_sha256"]
                    ):
                        raise ChatDataError("FOLLOWUP_EVIDENCE_CHANGED", 409)
                    old.append(r)
                if not old or chat.get("anchor_id") not in {
                    r["record_id"] for r in old
                }:
                    raise ChatDataError("INVALID_FOLLOWUP_ANCHOR", 422)
                trial = {**merged, **{r["record_id"]: r for r in old}}
                added = [
                    r
                    for k, r in trial.items()
                    if k not in {r["record_id"] for r in records}
                ]
                if len(packed(added).encode()) > 18000:
                    raise ChatDataError("FOLLOWUP_CONTEXT_BUDGET", 429)
                merged = trial
                existing.append(
                    {
                        "event_id": item["event_id"],
                        "title": item["payload"].get("title"),
                        "status": item["status"],
                        "latest_sent_at": item["latest_sent_at"],
                        "human_override": bool(item["human_override"]),
                        "anchor_id": chat.get("anchor_id"),
                        "issue_quote": chat.get("issue_quote"),
                        "time": chat.get("time"),
                        "evidence_record_ids": [r["record_id"] for r in old],
                    }
                )
            except ChatDataError as exc:
                unavailable.append({"event_id": item["event_id"], "error": exc.code})
            if len(existing) == 15:
                break
        if not page["has_more"]:
            break
        offset += 100
    return list(merged.values()), existing, unavailable


def verify_records(controller, records):
    from allday_asr.v3.domain.chat_identity import frozen_record

    for original in records:
        current = controller.chat.cache.record(original["record_id"])
        if current is not None and frozen_record(current) != frozen_record(original):
            raise ChatDataError("FOLLOWUP_EVIDENCE_CHANGED", 409)
