"""Original-source reads share the query budget; saved context stays explicitly qualified."""

import json
from .recall_investigation_budget import source_remaining
from allday_asr.v3.ports.chat_data import ChatDataError


def read_context(service, value, row):
    metrics = value["metrics"]
    with service.lock:
        left = source_remaining(metrics)
        if not all(left.values()):
            raise ChatDataError("SOURCE_REQUEST_BUDGET_REACHED", 429)
        count = min(7, left["remote_records"])
        before = min(2, (count - 1) // 2)
        after = min(4, count - 1 - before)
        metrics["source_requests"] += 1
        metrics["retrieval_requests"] = metrics.get("retrieval_requests", 0) + 1
        metrics["body_requests"] = (
            metrics.get("body_requests", metrics["retrieval_requests"] - 1) + 1
        )
    part = service.source.request(
        "/v1/context", {"record_id": row["record_id"], "before": before, "after": after}
    )
    records = part.get("items")
    size = len(json.dumps(part, ensure_ascii=False).encode())
    with service.lock:
        metrics["source_bytes"] = metrics.get("source_bytes", 0) + size
        if isinstance(records, list):
            metrics["source_records_read"] += len(records)
    if size > left["remote_bytes"]:
        raise ChatDataError("SOURCE_BYTE_BUDGET_REACHED", 429)
    if not isinstance(records, list) or len(records) > count:
        raise ChatDataError("INVALID_CHAT_CONTEXT", 502)
    return part


def saved_context(service, value, row):
    """No new source scan: only this query's exact, still-valid, scoped records."""
    known = {r["id"]: r for r in value["evidence"]}
    known.update(value.get("_context_evidence", {}))
    scope = value.get("_evidence_scopes", {}).get(row["id"], value["scope"])
    rows = [
        r
        for r in known.values()
        if all(
            r.get(k) == row.get(k)
            for k in ("source", "source_account_id", "conversation_id", "dataset")
        )
        and abs((r.get("sent_at") or 0) - (row.get("sent_at") or 0)) <= 1800
    ]
    raw = [
        {
            **r,
            "platform": r["source"],
            "is_from_self": r.get("source_is_from_self", r.get("is_from_self")),
        }
        for r in rows
    ]
    permitted = {r["id"] for r in service.sources.filter_context(raw, scope)}
    rows = [r for r in rows if r["id"] in permitted]
    invalid = service.sources.changed(rows)
    rows = [r for r in rows if r["id"] not in invalid and r.get("validity") != "stale"]
    rows.sort(key=lambda r: (r.get("sent_at") or 0, r["id"]))
    index = next(
        (
            i
            for i, r in enumerate(rows)
            if r["id"] == row["id"] and r["revision"] == row["revision"]
        ),
        None,
    )
    if index is None:
        return [row]
    return rows[max(0, index - 2) : index + 5]
