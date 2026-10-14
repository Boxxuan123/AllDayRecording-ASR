"""Bounded expansion decisions based on coverage and intent, not hit counts."""

from allday_asr.v3.ports.chat_data import ChatDataError


def expand(service, result, scope, query_plan, cancel, deadline):
    repo = service.sources
    scopes = repo.authorized_scopes(scope)
    if not scopes:
        result["source_status"] = "scope_unavailable"
        result["coverage"].append("未确认可用的历史来源会话；未扩大检索，来源覆盖不足")
        return
    if len(scopes) > 4:
        result["coverage"].append("本轮最多核对四个已确认历史会话，其他会话覆盖不足")

    def request(route, **kwargs):
        service._checkpoint(cancel, deadline)
        if (
            result["metrics"]["source_requests"] >= 6
            or result["metrics"]["source_records_read"] >= 100
        ):
            raise ChatDataError("SOURCE_REQUEST_BUDGET_REACHED", 429)
        result["metrics"]["source_requests"] += 1
        return service.source.request(route, **kwargs)

    try:
        status = request("/v1/status")
        if status.get("dataset_id") != repo.dataset():
            raise ChatDataError("DATASET_MISMATCH", 409)
        if status.get("status") in {"initializing", "not_acquired", "collecting"}:
            result["source_status"] = "not_collected"
            result["coverage"].append("Mac 来源尚未采集完成；本轮不能判断是否有聊天")
            return
        result["source_status"] = "available"
        if scope.get("relationship") == "conversation":
            conversation_expand(service, result, scope, query_plan, scopes, request)
            return
        # Unknown source completeness/freshness warrants one bounded probe even
        # with many local hits. Follow-up probes drop keywords but retain the exact
        # conversation and dates; other senders remain independent context.
        jobs = []
        local = [r for r in result["evidence"] if r["source"] != "recording"]
        conversations = {}
        for row in local:
            key = (row["source"], row["source_account_id"], row["conversation_id"])
            conversations.setdefault(key, []).append(row)
        for (platform, account, conversation), rows in list(conversations.items())[:2]:
            authorized = any(
                s.get("platform") == platform
                and s.get("source_account_id") == account
                and (
                    not s.get("conversation_id") or s["conversation_id"] == conversation
                )
                for s in scopes
            )
            if not authorized:
                continue
            newest = max(r["sent_at"] for r in rows)
            jobs.append(
                (
                    {
                        "platform": platform,
                        "source_account_id": account,
                        "conversation_id": conversation,
                        "sent_from": max(scope["sent_from"], newest),
                        "sent_to": scope["sent_to"],
                    },
                    "followup",
                )
            )
        # Latest/timeline prioritize new follow-ups; cause/quote also check anchors.
        seed = [
            (
                {
                    **s,
                    "sent_from": scope["sent_from"],
                    "sent_to": scope["sent_to"],
                    "keyword": term,
                },
                "match",
            )
            for s in scopes[:2]
            for term in query_plan["remote_terms"][:2]
        ]
        if query_plan["intent"] in {"latest", "timeline"}:
            jobs = jobs + seed
        else:
            jobs = seed[:2] + jobs
        seen = set()
        for remote, role in jobs:
            fingerprint = str(sorted(remote.items()))
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            limit = min(40, 100 - result["metrics"]["source_records_read"])
            if limit <= 0:
                break
            page = request("/v1/messages", scope=remote, limit=limit)
            records = page.get("items")
            if not isinstance(records, list) or len(records) > limit:
                raise ChatDataError("INVALID_CHAT_PAGE", 502)
            result["metrics"]["source_records_read"] += len(records)
            for rec in records:
                repo.validate_record(rec)
                if (
                    any(
                        rec.get(k) != v
                        for k, v in remote.items()
                        if k
                        in {
                            "platform",
                            "source_account_id",
                            "conversation_id",
                            "sender_account_id",
                        }
                    )
                    or not scope["sent_from"]
                    <= rec.get("sent_at", -1)
                    <= scope["sent_to"]
                ):
                    raise ChatDataError("CONTEXT_IDENTITY_MISMATCH", 502)
                if (
                    scope.get("relationship") == "group"
                    and rec.get("conversation_type") != "group"
                ):
                    continue
                evidence = repo.evidence(rec, "mac", role)
                if role == "match" and (
                    scope["person_id"]
                    and evidence["person_id"] != scope["person_id"]
                    or not all(
                        t.casefold() in (rec.get("text") or "").casefold()
                        for t in query_plan["terms"]
                    )
                ):
                    continue
                old = next(
                    (r for r in result["evidence"] if r["id"] == evidence["id"]), None
                )
                if old and evidence["revision"] > old["revision"]:
                    repo.mark_changed(old, "SOURCE_CHANGED")
                if old and evidence["revision"] < old["revision"]:
                    continue
                if (
                    role == "followup"
                    and all(
                        t.casefold() in evidence["quote"].casefold()
                        for t in query_plan["terms"]
                    )
                    and (
                        not scope["person_id"]
                        or evidence["person_id"] == scope["person_id"]
                    )
                ):
                    evidence["role"] = "match"
                result["evidence"].append(evidence)
            if page.get("has_more"):
                result["coverage"].append(
                    "Mac 仍有后续页；本轮有界取回，不能确认最终状态"
                )
                result.setdefault("continuations", []).append(
                    {"scope": remote, "cursor": page.get("next_cursor")}
                )
        result["coverage"].append(
            "已按当前意图核对部分来源后续；捕获完整性与更晚状态仍未知"
        )
    except ChatDataError as exc:
        result["coverage"].append(
            "来源未完整取回：" + exc.code + "；缓存不代表当前最终状态"
        )
        result["source_status"] = "partial" if exc.status == 429 else "unavailable"


def contexts(service, result, cancel, deadline):
    for row in list(result["evidence"])[:3]:
        if row["source"] == "recording" or row["origin"] != "cache":
            continue
        service._checkpoint(cancel, deadline)
        part = service.sources.context(row, result["scope"])
        existing = {r["id"] for r in result["evidence"]}
        result["evidence"].extend(r for r in part if r["id"] not in existing)
    result["coverage"].append(
        "邻近上下文最多 3 个命中各前 2／后 4 条；其他发言者保留独立身份。未覆盖的更远答复仍未知。"
    )


def conversation_expand(service, result, scope, plan, scopes, request):
    records = []
    jobs = [(selected, None) for selected in scopes[:4]]
    error = None
    seen = set()
    try:
        while jobs:
            selected, cursor = jobs.pop(0)
            if result["metrics"]["source_requests"] >= 6 or len(records) >= 100:
                result["source_status"] = "partial"
                result["coverage"].append("达到本轮分页/记录上限，仍有会话未完整取回")
                break
            fingerprint = (tuple(sorted(selected.items())), str(cursor))
            if fingerprint in seen:
                raise ChatDataError("INVALID_CHAT_CURSOR", 502)
            seen.add(fingerprint)
            remote = {
                **selected,
                "sent_from": scope["sent_from"],
                "sent_to": scope["sent_to"],
            }
            limit = min(40, 100 - len(records))
            options = {"scope": remote, "limit": limit}
            if cursor is not None:
                options["cursor"] = cursor
            page = request("/v1/messages", **options)
            items = page.get("items")
            if not isinstance(items, list) or len(items) > limit:
                raise ChatDataError("INVALID_CHAT_PAGE", 502)
            result["metrics"]["source_records_read"] += len(items)
            for record in items:
                service.sources.validate_record(record)
                if (
                    any(record.get(k) != v for k, v in selected.items())
                    or not scope["sent_from"]
                    <= record.get("sent_at", -1)
                    <= scope["sent_to"]
                ):
                    raise ChatDataError("CONTEXT_IDENTITY_MISMATCH", 502)
            records.extend(items)
            if page.get("has_more"):
                result["source_status"] = "partial"
                result["coverage"].append(
                    "该会话返回部分记录；尚未取完的页不代表没有聊天"
                )
                next_cursor = page.get("next_cursor")
                if not isinstance(next_cursor, str) or not next_cursor:
                    break
                jobs.append((selected, next_cursor))
    except ChatDataError as exc:
        error = exc
    evidence = service.sources.conversation_evidence(records, scope, result["evidence"])
    if plan["terms"]:
        keys = {
            (r["source"], r["source_account_id"], r["conversation_id"])
            for r in [*result["evidence"], *evidence]
            if all(t.casefold() in r["quote"].casefold() for t in plan["terms"])
        }
        evidence = [
            r
            for r in evidence
            if (r["source"], r["source_account_id"], r["conversation_id"]) in keys
        ]
    for row in evidence:
        old = next((r for r in result["evidence"] if r["id"] == row["id"]), None)
        if old and row["revision"] > old["revision"]:
            service.sources.mark_changed(old, "SOURCE_CHANGED")
        if not old or row["revision"] >= old["revision"]:
            result["evidence"].append(row)
    result["coverage"].append(
        "按已确认历史会话取回双方消息；本地查询日无缓存也可请求，人物/日期/来源不扩大。Mac 已采集范围和完整性仍未知。"
    )
    if not records:
        result["source_status"] = "coverage_unknown"
        result["coverage"].append(
            "Mac 在本次范围内未返回消息；可能尚未采集，不能判断没有聊天"
        )
    if error:
        raise error
