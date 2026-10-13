"""Bounded expansion decisions based on coverage and intent, not hit counts."""

from allday_asr.v3.ports.chat_data import ChatDataError
from allday_asr.v3.domain.recall_language import relevance


def expand(service, result, scope, query_plan, cancel, deadline):
    repo = service.sources
    scopes = repo.authorized_scopes(scope)
    if not scopes:
        result["coverage"].append("未确认可用的来源身份或未登记范围；未扩大检索")
        return
    if len(scopes) > 2:
        result["coverage"].append("跨来源取回仅覆盖前两个已授权身份／会话")

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
        result["source_status"] = "available"
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
                evidence = repo.evidence(rec, "mac", role)
                if role == "match" and (
                    scope["person_id"]
                    and evidence["person_id"] != scope["person_id"]
                    or remote.get("keyword", "") not in (rec.get("text") or "")
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
                    and relevance(evidence["quote"], query_plan["terms"]) > 0
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
