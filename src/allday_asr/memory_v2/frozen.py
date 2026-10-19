"""Queryable read-only native snapshot. No answer ID lookup or evaluation labels."""

import copy
import json
from allday_asr.v3.ports.chat_data import ChatDataError


class FrozenSnapshot:
    def __init__(self, snapshot):
        self.records = copy.deepcopy(snapshot["records"])
        self.dataset = snapshot["dataset_id"]
        self.status = copy.deepcopy(snapshot["source_status"])
        self.snapshots = {}
        self.failure = None
        self.calls = []

    def cursor(self, kind, scope, after, boundary):
        return json.dumps([kind, self.dataset, scope, after, boundary], sort_keys=True)

    def read_cursor(self, cursor, kind, scope):
        ck, dataset, old, after, boundary = json.loads(cursor)
        if dataset != self.dataset:
            raise ChatDataError("DATASET_MISMATCH", 409)
        if old != scope:
            raise ChatDataError("SCOPE_MISMATCH", 409)
        if ck != kind:
            raise ChatDataError("CURSOR_KIND_MISMATCH", 409)
        return after, boundary

    @staticmethod
    def match(r, scope):
        for key, value in scope.items():
            if key == "keyword" and value not in (r["text"] or ""):
                return False
            if key == "sent_from" and (r["sent_at"] is None or r["sent_at"] < value):
                return False
            if key == "sent_to" and (r["sent_at"] is None or r["sent_at"] > value):
                return False
            if key not in {"keyword", "sent_from", "sent_to"} and r[key] != value:
                return False
        return True

    def request(self, route, body=None, **query):
        self.calls.append((route, copy.deepcopy(body), copy.deepcopy(query)))
        if self.failure:
            raise ChatDataError(self.failure)
        scope = query.get("scope", {})
        if route == "/v1/status":
            return copy.deepcopy(self.status)
        if route == "/v1/contract":
            return {
                "api_contract_version": "CHAT_DATA_CONTRACT_V1",
                "schema_version": 1,
                "revision": "inserts only",
            }
        if route == "/v1/coverage":
            return {
                "dataset_id": self.dataset,
                "sources": [
                    {
                        "platform": p,
                        "changes": ["insert"],
                        "updates": "unsupported",
                        "deletes": "unsupported",
                        "last_capture_at": "2026-10-09T00:00:00Z",
                    }
                    for p in ["qq", "wechat"]
                ],
                "historical_continuity": "unknown",
            }
        if route == "/v1/snapshots":
            scope = body["scope"]
            boundary = len(self.records)
            sid = "snapshot-" + str(len(self.snapshots))
            self.snapshots[sid] = (copy.deepcopy(scope), boundary)
            return {
                "snapshot_id": sid,
                "boundary_cursor": self.cursor("change", scope, boundary, None),
                "scope": scope,
                "expires_at": "2026-10-10T00:00:00Z",
            }
        if route in {"/v1/messages", "/v1/changes"}:
            kind = "change" if route.endswith("changes") else "query"
            after, boundary = 0, len(self.records)
            if query.get("cursor"):
                after, boundary = self.read_cursor(query["cursor"], kind, scope)
                if kind == "change":
                    boundary = len(self.records)
            if query.get("snapshot_id"):
                old, boundary = self.snapshots[query["snapshot_id"]]
                if old != scope:
                    raise ChatDataError("SCOPE_MISMATCH", 409)
            pairs = [
                (i + 1, r)
                for i, r in enumerate(self.records)
                if after < i + 1 <= boundary and self.match(r, scope)
            ]
            limit = query.get("limit", 100)
            selected = pairs[:limit]
            more = len(pairs) > limit
            end = selected[-1][0] if more else boundary
            items = [copy.deepcopy(r) for _, r in selected]
            if kind == "change":
                items = [
                    {
                        "operation": "insert",
                        "record_id": r["record_id"],
                        "record_revision": r["record_revision"],
                        "change_seq": i,
                        "record": r,
                    }
                    for (i, _), r in zip(selected, items, strict=True)
                ]
            return {
                "items": items,
                "has_more": more,
                "next_cursor": self.cursor(
                    kind, scope, end, None if kind == "change" else boundary
                ),
                "scope": scope,
                "order": "change_seq",
            }
        if route == "/v1/context":
            anchor = next(
                (r for r in self.records if r["record_id"] == body["record_id"]), None
            )
            if anchor is None:
                raise ChatDataError("RECORD_NOT_FOUND", 404)
            items = sorted(
                [
                    r
                    for r in self.records
                    if all(
                        r[k] == anchor[k]
                        for k in ["platform", "source_account_id", "conversation_id"]
                    )
                ],
                key=lambda r: (r["sent_at"] or -1, r["record_id"]),
            )
            at = items.index(anchor)
            before = body.get("before", 10)
            after = body.get("after", 10)
            return {
                "items": copy.deepcopy(items[max(0, at - before) : at + after + 1]),
                "anchor_record_id": anchor["record_id"],
                "truncated_before": at > before,
                "truncated_after": len(items) > at + after + 1,
                "references": [],
                "reference_status": "unresolved" if anchor["reply"] else "none",
            }
        if route in {"/v1/accounts", "/v1/conversations"}:
            values = {}
            for r in self.records:
                if any(r[k] != v for k, v in scope.items()):
                    continue
                if route.endswith("accounts"):
                    for role, key in [
                        ("source", "source_account_id"),
                        ("sender", "sender_account_id"),
                    ]:
                        ident = r[key]
                        values[(r["platform"], role, ident)] = {
                            "id": ident,
                            "role": role,
                            "platform": r["platform"],
                            "display_name": r["sender_display_name"]
                            if role == "sender"
                            else ident,
                        }
                else:
                    values[
                        (r["platform"], r["source_account_id"], r["conversation_id"])
                    ] = {
                        "id": r["conversation_id"],
                        "platform": r["platform"],
                        "source_account_id": r["source_account_id"],
                        "display_name": r["conversation_display_name"],
                        "conversation_type": r.get("conversation_type", "group"),
                    }
            return {
                "items": list(values.values()),
                "next_cursor": None,
                "has_more": False,
            }
        if route == "/v1/records":
            result = []
            for item in body["items"]:
                r = next(
                    (
                        r
                        for r in self.records
                        if r["record_id"] == item["record_id"]
                        and all(
                            r[k] == item[k]
                            for k in (
                                "platform",
                                "source_account_id",
                                "conversation_id",
                            )
                            if k in item
                        )
                    ),
                    None,
                )
                state = (
                    "not_found"
                    if r is None
                    else "version_unavailable"
                    if item.get("record_revision", 1) != r["record_revision"]
                    else "found"
                )
                result.append(
                    {
                        "request": item,
                        "status": state,
                        "record": r if state == "found" else None,
                    }
                )
            return {"items": result}
        raise ChatDataError("NOT_FOUND", 404)
