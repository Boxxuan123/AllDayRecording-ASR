"""Complete a fixed snapshot with durable page checkpoints and no row/page cap."""

import json
import time
from allday_asr.v3.domain.chat_data import scope_key
from allday_asr.v3.ports.chat_data import ChatDataError


def collect(controller, value, job_id=None):
    cache = controller.chat.cache
    configured, tz = controller.scopes()
    if not configured:
        raise ChatDataError("FOLLOWUP_SCOPE_REQUIRED", 400)
    dataset = controller.chat.discover(force=True)["dataset_id"]
    if value.get("dataset") and value["dataset"] != dataset:
        raise ChatDataError("FOLLOWUP_SOURCE_CHANGED", 409)
    previous = [c for c in value.get("coverage", []) if "scope" in c]
    now = int(time.time())
    window = value.setdefault(
        "collection_window",
        {
            "sent_from": previous[0]["scope"]["sent_from"]
            if previous
            else now - 7 * 86400,
            "sent_to": previous[0]["scope"]["sent_to"] if previous else now,
        },
    )
    raw_scopes = value.setdefault("collection_scopes", configured)
    scopes = [
        {
            **{
                k: s[k]
                for k in ("platform", "source_account_id", "conversation_id")
                if k in s
            },
            **window,
        }
        for s in raw_scopes
    ]

    def selected(conversation):
        platform, source, conv = json.loads(conversation)
        return any(
            s["platform"] == platform
            and s["source_account_id"] == source
            and ("conversation_id" not in s or s["conversation_id"] == conv)
            for s in scopes
        )

    records = {r["record_id"]: r for r in value.get("records") or []}
    coverage = [{"kind": "existing_open_context", "complete": False}]
    for scope in scopes:
        prior = next((c for c in previous if c["scope"] == scope), None)
        coverage.append(
            dict(prior)
            if prior
            else {
                "scope": scope,
                "received": 0,
                "complete": False,
                "reason": "尚未读取",
            }
        )
    value.update(dataset=dataset, records=list(records.values()), coverage=coverage)
    value.setdefault("timezone", tz)

    def checkpoint():
        value["records"] = list(records.values())
        value["unique_messages"] = len(records)
        if job_id is not None:
            controller.service.save_job(job_id, "collecting", value)

    def cancelled():
        if controller._cancel.is_set():
            raise ChatDataError("CANCELLED", 409)

    checkpoint()
    offset = 0
    while True:
        cancelled()
        page = controller.service.list(100, offset)
        for item in page["items"]:
            if (
                item["dataset"] == dataset
                and item["status"] == "active"
                and selected(item["conversation_key"])
            ):
                for r in item["chat"].get("evidence", []):
                    current = cache.record(r["record_id"])
                    if current and current["record_revision"] == r["record_revision"]:
                        records[r["record_id"]] = current
                    else:
                        raise ChatDataError("FOLLOWUP_EVIDENCE_UNAVAILABLE", 409)
        if not page["has_more"]:
            break
        if not page["items"]:
            raise ChatDataError("FOLLOWUP_CURSOR_STALLED", 502)
        offset += len(page["items"])
    coverage[0]["complete"] = True
    checkpoint()
    for entry in coverage[1:]:
        cancelled()
        scope = entry["scope"]
        sid = entry.get("receipt_scope_id") or scope_key(
            {"dataset": dataset, "scope": scope}
        )
        prior = cache.scope(sid)
        if prior:
            if prior["dataset"] != dataset or prior["scope"] != scope:
                raise ChatDataError("FOLLOWUP_SOURCE_CHANGED", 409)
            snapshot = {
                "snapshot_id": prior["snapshot"],
                "boundary_cursor": prior["boundary"],
            }
        else:
            snapshot = controller.chat.source.request("/v1/snapshots", {"scope": scope})
            sid = cache.create_scope(dataset, scope, snapshot)
            cache.enable_scope(sid, False)
            prior = cache.scope(sid)
        entry.update(
            snapshot_id=snapshot["snapshot_id"],
            boundary_cursor=snapshot["boundary_cursor"],
            receipt_scope_id=sid,
        )
        # Recover atomic cache receipts before trusting their cursor. The worker
        # may have died after receive but before the separate job checkpoint.
        with cache.connect() as db:
            captured = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT r.body FROM scope_records s JOIN records r ON r.id=s.record_id AND r.dataset=? WHERE s.scope_id=? AND r.deleted=0",
                    (dataset, sid),
                )
            ]
        for r in captured:
            records[r["record_id"]] = r
        entry["received"] = len(captured)
        entry["complete"] = prior["phase"] == "changes"
        cursor = prior["page_cursor"]
        entry["next_cursor"] = None if entry["complete"] else cursor
        entry["reason"] = None if entry["complete"] else "正在读取后续页"
        checkpoint()
        visited = {cursor}
        while not entry["complete"]:
            cancelled()
            page = controller.chat.source.request(
                "/v1/messages",
                scope=scope,
                cursor=cursor,
                limit=100,
                snapshot_id=snapshot["snapshot_id"],
            )
            controller.chat._page(page)
            if page["has_more"] and (
                not page["items"] or page["next_cursor"] in visited
            ):
                raise ChatDataError("FOLLOWUP_CURSOR_STALLED", 502)
            done = not page["has_more"]
            cache.receive(
                dataset,
                page["items"],
                scope_id=sid,
                progress={
                    "page_cursor": page["next_cursor"],
                    "receive_cursor": snapshot["boundary_cursor"] if done else None,
                    "phase": "changes" if done else "snapshot",
                },
            )
            for r in page["items"]:
                records[r["record_id"]] = r
            cursor = page["next_cursor"]
            visited.add(cursor)
            entry.update(
                received=cache.scope(sid)["received"],
                complete=done,
                next_cursor=None if done else cursor,
                reason=None if done else "正在读取后续页",
            )
            checkpoint()
    value["collection_complete"] = True
    checkpoint()
