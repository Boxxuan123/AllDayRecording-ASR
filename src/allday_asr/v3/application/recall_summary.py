"""Citation-checked single-call summaries, including honest partial-dialogue states."""

import time
from .recall_citation_handles import CitationHandles
from .recall_investigation_budget import normalize_scope
from allday_asr.v3.domain.recall_answer import verified_quote_text
from allday_asr.v3.domain.recall_language import select_evidence


def summarize(service, result, question, cancel, deadline):
    if (
        result.get("_spent_ms") is not None
        and result["metrics"]["model_calls"] >= result["limits"]["model_calls"]
    ):
        result.update(
            model_status="insufficient",
            summary_retry_available=False,
            answer="本次调查的模型预算已用完，已按所选对象取回原文，尚未生成交流归纳。",
            unknown=["身份选择不会重置本次查询预算；当前原文尚未归纳。"],
        )
        return
    # Exact quotes remain available on every model failure. One invocation, no retries.
    if (
        result["scope"].get("source_contacts")
        and result["scope"].get("relationship") == "conversation"
        and {r.get("effective_is_self") for r in result["evidence"]} != {False, True}
    ):
        result["coverage"].append(
            "仅取得单方消息；可归纳该方明确说过的内容，不能补写另一方答复或确认完整交流。"
        )
    rows = select_evidence(result["evidence"], result["intent"], result["terms"], 20)
    if len(result["evidence"]) > 20:
        result["coverage"].append(
            "归纳最多使用 20 条按相关性、时间跨度和状态变化选出的依据；其余原文仍可翻页"
        )
    normalize_scope(result["scope"])
    handles = CitationHandles(result)
    encoded = handles.encode(rows)
    handles.validate(encoded,{r["id"]:r for r in result["evidence"]})
    result["metrics"].setdefault("model_inputs", []).append(handles.receipt(encoded))
    payload = {
        "question": question,
        "query_reference": result["_query_reference"],
        "local_now": result["_query_reference"]["local"],
        "query_id": result["query_id"],
        "intent": result["intent"],
        "model_budget_seconds": min(45, int(deadline - time.monotonic())),
        "summary_attempt": result.get("_summary_attempt", 0),
        "scope": result["scope"],
        "requested_scope": normalize_scope(result.get("requested_scope", result["scope"])),
        "time_notice": result.get("time_notice", ""),
        "records": encoded,
        "evidence_complete": False,
        "known_gaps": result["coverage"],
    }
    result["metrics"]["model_input_ids"] = [r["id"] for r in rows]
    started = time.monotonic()
    try:
        if payload["model_budget_seconds"] < 2:
            raise TimeoutError()
        result["metrics"]["model_calls"] += 1
        value = handles.decode(service.model(payload, cancel))
        service._checkpoint(cancel, deadline)
        evidence = {r["id"]: r for r in rows}
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("claims"), list)
            or len(value["claims"]) > 20
            or not isinstance(value.get("unknown"), list)
            or len(value["unknown"]) > 20
        ):
            raise ValueError("invalid answer")
        for claim in value["claims"]:
            if (
                set(claim) != {"kind", "text", "citations"}
                or claim["kind"] not in {"quote", "summary", "inference"}
                or not isinstance(claim["text"], str)
                or not claim["text"].strip()
                or len(claim["text"]) > 2000
                or not isinstance(claim["citations"], list)
                or not claim["citations"]
                or any(cid not in evidence for cid in claim["citations"])
            ):
                raise ValueError("invalid citation")
            if claim["kind"] == "quote":
                claim["text"] = verified_quote_text(
                    claim["text"],
                    [evidence[cid]["quote"] for cid in claim["citations"]],
                )
        if any(not isinstance(v, str) or len(v) > 2000 for v in value["unknown"]):
            raise ValueError("invalid unknown")
        result["claims"] = value["claims"]
        result["unknown"] = value["unknown"]
        result["model_status"] = "available" if value["claims"] else "insufficient"
        result["summary_retry_available"] = False
        # The primary answer is derived only from citation-validated claims.
        result["answer"] = "\n".join(
            ("推测：" + c["text"]) if c["kind"] == "inference" else c["text"]
            for c in value["claims"]
        ) or ("现有证据不足，无法生成可靠归纳；请核对本次范围内的原文。")
    except Exception as exc:
        result["model_status"] = (
            "timeout"
            if isinstance(exc, TimeoutError)
            else "failed"
            if isinstance(exc, ValueError)
            or getattr(exc, "code", "") == "INVALID_MODEL_QUOTE"
            else "unavailable"
        )
        result["claims"] = []
        reason = {
            "timeout": "归纳超时",
            "failed": "归纳的依据校验未通过",
            "unavailable": "归纳模型暂不可用",
        }[result["model_status"]]
        result["unknown"] = [reason + "，本轮未形成内容结论。"]
        result["answer"] = (
            "无法生成归纳：" + reason + "。以下为符合本次联系人、时间与会话约束的原文。"
        )
        result["summary_retry_available"] = (
            not cancel.is_set()
            and result.get("_summary_attempt", 0) == 0
            and "_spent_ms" not in result
        )
    finally:
        result["metrics"]["model_elapsed_ms"] = round(
            (time.monotonic() - started) * 1000
        )
