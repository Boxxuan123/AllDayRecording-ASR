"""Explicit recovery actions over owned queries; no business identity writes."""

import time
from copy import deepcopy
from threading import Event
from allday_asr.v3.domain.recall_investigation_scope import platform_allowed


def lookup_contacts(service, owner, payload):
    if set(payload) - {"action", "query_id", "search", "offset"}:
        raise ValueError("联系人查询字段无效")
    saved = service._saved(owner, payload.get("query_id"))
    if not saved.get("contact_lookup_allowed") or not service.directory:
        raise ValueError("请先明确日期或人物筛选；本轮不能改变这些约束")
    text, offset = payload.get("search", ""), payload.get("offset", 0)
    if (
        not isinstance(text, str)
        or len(text) > 100
        or type(offset) is not int
        or not 0 <= offset <= 4980
    ):
        raise ValueError("联系人搜索条件无效")
    key = (owner, saved["query_id"])
    cancel = Event()
    with service.lock:
        if key in service.active or len(service.active) >= 2:
            raise ValueError("查询正在执行，请稍后重试")
        service.active[key] = cancel
        # Two explicit metadata refreshes, six requests each. Further search
        # and paging use the catalogue; message bodies are never requested.
        live = saved.get("_lookup_refreshes", 0) < 2 and saved["_request"].get(
            "expand", True
        )
        if live:
            saved["_lookup_refreshes"] = saved.get("_lookup_refreshes", 0) + 1
    try:
        rows, notes, calls = service.directory.load(
            saved["scope"]["source"], live, cancel, time.monotonic() + 12
        )
        service._checkpoint(cancel, time.monotonic() + 1)
        rows = [r for r in rows if platform_allowed(saved["scope"], r["platform"])]
        if text.strip():
            # Explicit user directory search, not speculative name matching.
            rows = [
                r
                for r in rows
                if any(text.strip().casefold() in n.casefold() for n in r["names"])
            ]
        rows.sort(key=lambda r: (r["platform"], r["display_name"], r["key"]))
        candidates = rows[offset : offset + 20]
        saved["_lookup_candidates"] = deepcopy(candidates)
        saved["metrics"]["directory_requests"] += calls
        saved["metrics"]["source_requests"] += calls
        return deepcopy(
            {
                "query_id": saved["query_id"],
                "contact_candidates": candidates,
                "has_more": offset + 20 < len(rows),
                "next_offset": offset + 20,
                "notice": "只列出实际来源联系人。选择后继续原问题，仅本次有效。"
                + (
                    "当前目录尚未完整覆盖或来源离线；找不到不表示联系人不存在。"
                    if notes
                    else ""
                ),
                "coverage": notes,
            }
        )
    finally:
        with service.lock:
            service.active.pop(key, None)


def retry_summary(service, owner, payload):
    if set(payload) != {"action", "query_id"}:
        raise ValueError("归纳重试字段无效")
    saved = service._saved(owner, payload.get("query_id"))
    key = (owner, saved["query_id"])
    cancel = Event()
    with service.lock:
        if key in service.active or len(service.active) >= 2:
            raise ValueError("查询正在执行，请稍后重试")
        if (
            not saved.get("summary_retry_available")
            or saved.get("status") == "stale"
            or any(r.get("validity") == "stale" for r in saved["evidence"])
        ):
            raise ValueError("本次不能重试归纳，请重新查询以核对原文")
        saved["summary_retry_available"] = False
        saved["_summary_attempt"] = 1
        service.active[key] = cancel
        result = deepcopy(saved)
    try:
        service._summarize(
            result, result["_request"]["question"], cancel, time.monotonic() + 50
        )
        if cancel.is_set():
            return {"status": "cancelled", "query_id": saved["query_id"]}
        service._checkpoint(cancel, time.monotonic() + 1)
        # A correction during inference must not revive an old conclusion.
        service._saved(owner, saved["query_id"])
        if service.sources.identity_changed(result["evidence"]):
            raise ValueError("来源身份已变化，请重新查询")
        service._invalidate(result, service.sources.changed(result["evidence"]))
        with service.lock:
            if key in service.cancelled or cancel.is_set():
                return {"status": "cancelled", "query_id": saved["query_id"]}
            service.results[key] = (time.monotonic(), result)
        return service.page(owner, saved["query_id"])
    finally:
        with service.lock:
            service.active.pop(key, None)


def target_preference(service, owner, payload):
    saved = service._saved(owner, payload.get("query_id"))
    if set(payload) != {"action", "query_id"} or not service.preferences:
        raise ValueError("查询偏好操作无效")
    if payload["action"] == "remember_target":
        contacts = saved["scope"].get("source_contacts", [])
        mention = saved["_plan"]["person_mention"]
        if not mention or len(contacts) != 1 or saved.get("status") == "stale":
            raise ValueError("当前查询没有可记忆的唯一来源对象")
        saved["target_preference"] = service.preferences.remember(
            owner, mention, contacts[0]
        )
        saved["target_remember_available"] = False
    else:
        preference = saved.get("target_preference")
        if preference:
            service.preferences.forget(owner, preference["mention"])
            saved.pop("target_preference", None)
            saved["_plan"].pop("target_preference", None)
            saved["target_remember_available"] = True
    return service.page(owner, saved["query_id"])
