"""Citation-checked single-call summaries, including honest partial-dialogue states."""

import time
from allday_asr.v3.domain.recall_answer import verified_quote_text
from allday_asr.v3.domain.recall_language import select_evidence


def summarize(service, result, question, cancel, deadline):
    # Exact quotes remain available on every model failure. One invocation, no retries.
    if (
        result["scope"].get("source_contacts")
        and result["scope"].get("relationship") == "conversation"
        and {r.get("effective_is_self") for r in result["evidence"]} != {False, True}
    ):
        result["model_status"] = "insufficient"
        result["claims"] = []
        result["unknown"] = ["本次尚未取回双方消息，无法确认完整交流。"]
        result["answer"] = (
            "尚未取回双方消息，暂时无法归纳完整交流；以下是本次联系人、时间与会话范围内的原文。"
        )
        return
    rows = select_evidence(result["evidence"], result["intent"], result["terms"], 20)
    if len(result["evidence"]) > 20:
        result["coverage"].append(
            "归纳最多使用 20 条按相关性、时间跨度和状态变化选出的依据；其余原文仍可翻页"
        )
    payload = {
        "question": question,
        "query_id": result["query_id"],
        "intent": result["intent"],
        "model_budget_seconds": min(45, int(deadline - time.monotonic())),
        "summary_attempt": result.get("_summary_attempt", 0),
        "scope": result["scope"],
        "records": rows,
        "evidence_complete": False,
        "known_gaps": result["coverage"],
    }
    result["metrics"]["model_input_ids"] = [r["id"] for r in rows]
    started = time.monotonic()
    try:
        if payload["model_budget_seconds"] < 2:
            raise TimeoutError()
        result["metrics"]["model_calls"] += 1
        value = service.model(payload, cancel)
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
            not cancel.is_set() and result.get("_summary_attempt", 0) == 0
        )
    finally:
        result["metrics"]["model_elapsed_ms"] = round(
            (time.monotonic() - started) * 1000
        )
