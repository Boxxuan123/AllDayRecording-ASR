"""Physical read accounting for the existing investigation tools."""

import json
import time
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo
from allday_asr.v3.domain.recall_investigation import window_scope
from allday_asr.v3.ports.chat_data import ChatDataError


def normalize_scope(scope):
    for key in ("sent_from", "sent_to"):
        scope[key + "_local"] = datetime.fromtimestamp(
            scope[key], ZoneInfo(scope["timezone"])
        ).isoformat()
    return scope


def source_remaining(metrics):
    """Shared physical limits for investigation and original-source UI reads."""
    return {
        "source_requests": max(0, 18 - metrics.get("source_requests", 0)),
        "body_requests": max(
            0, 12 - metrics.get("body_requests", metrics.get("retrieval_requests", 0))
        ),
        "remote_records": max(0, 200 - metrics.get("source_records_read", 0)),
        "remote_bytes": max(0, 1048576 - metrics.get("source_bytes", 0)),
    }


class InvestigationBudget:
    def normalize_scopes(self):
        normalize_scope(self.root_scope)

    def remaining(self):
        m = self.result["metrics"]
        from allday_asr.v3.domain.recall_read_budget import CURRENT

        scan = CURRENT.get()
        return {
            **source_remaining(m),
            "model_calls": max(0, 4 - m.get("model_calls", 0)),
            "seconds": max(0, round(self.deadline - time.monotonic(), 2)),
            "cache_records": max(
                0,
                min(
                    400 - m.get("cache_records_read", 0),
                    400 - scan.records if scan else 400,
                ),
            ),
            "cache_bytes": max(
                0,
                min(
                    524288 - m.get("cache_bytes", 0),
                    524288 - scan.bytes if scan else 524288,
                ),
            ),
        }

    def remote(self, route, body=None, **params):
        self.checkpoint()
        m = self.result["metrics"]
        is_body = route in {"/v1/messages", "/v1/context", "/v1/records"}
        left = self.remaining()
        if not left["source_requests"] or (
            is_body
            and not all(
                left[k] for k in ("body_requests", "remote_records", "remote_bytes")
            )
        ):
            raise ChatDataError("SOURCE_REQUEST_BUDGET_REACHED", 429)
        if route == "/v1/messages":
            params["limit"] = min(params.get("limit", 20), left["remote_records"])
        m["source_requests"] += 1
        m["retrieval_requests"] += 1
        m["body_requests"] = m.get("body_requests", 0) + int(is_body)
        trace = {"route": route, "body": body, "params": deepcopy(params)}
        self.result.setdefault("source_queries", []).append(trace)
        started = time.monotonic()
        try:
            value = self.service.source.request(route, body, **params)
            size = len(json.dumps(value, ensure_ascii=False).encode())
            m["source_bytes"] = m.get("source_bytes", 0) + size
            trace.update(
                records=len(value.get("items", [])),
                bytes=size,
                has_more=value.get("has_more", False),
            )
            if size > left["remote_bytes"]:
                raise ChatDataError("SOURCE_BYTE_BUDGET_REACHED", 429)
            for row in value.get("items", []):
                if isinstance(row, dict) and row.get("record_id"):
                    self.flow.received([row], "remote", route)
            self.checkpoint()
            return value
        except Exception as exc:
            trace["error"] = getattr(exc, "code", type(exc).__name__)
            raise
        finally:
            trace["elapsed_ms"] = round((time.monotonic() - started) * 1000)

    def verify_remote(self):
        if not self.remote_checked:
            status = self.remote("/v1/status")
            if status.get("dataset_id") != self.service.sources.dataset():
                raise ChatDataError("DATASET_MISMATCH", 409)
            if status.get("status") != "ready":
                raise ChatDataError("SOURCE_NOT_READY", 503)
            self.remote_checked = True

    def scope(self, window):
        value = window_scope(
            self.result["scope"], window, self.result["_request"], self.now
        )
        normalize_scope(value)
        if window != "target":
            self.note(
                f"{window} 计划搜索窗口：{value['sent_from_local']} 至 {value['sent_to_local']}；尚不表示取回了相关原文。"
            )
        self.result.setdefault("attempted_windows", []).append(
            {
                "window": window,
                **{
                    k: value[k]
                    for k in (
                        "sent_from",
                        "sent_to",
                        "sent_from_local",
                        "sent_to_local",
                    )
                },
            }
        )
        return self.bounded_scope(value)

    def cache_rows(self, rows):
        m = self.result["metrics"]
        kept = []
        for row in rows:
            size = len(json.dumps(row, ensure_ascii=False).encode())
            if (
                m.get("cache_records_read", 0) >= 400
                or m.get("cache_bytes", 0) + size > 524288
            ):
                self.note("缓存读取达到 400 条/512KB 上限。")
                break
            m["cache_records_read"] = m.get("cache_records_read", 0) + 1
            m["cache_bytes"] = m.get("cache_bytes", 0) + size
            kept.append(row)
        return kept
