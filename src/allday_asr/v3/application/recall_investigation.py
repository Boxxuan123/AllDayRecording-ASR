"""Two-decision evidence investigation, sharing HistoryRecall's lifecycle and limits."""

from copy import deepcopy
from datetime import date, datetime
from zoneinfo import ZoneInfo
import json
import time
from allday_asr.v3.domain.recall_investigation import checked, date_scope
from allday_asr.v3.ports.chat_data import ChatDataError
from .recall_investigation_tools import InvestigationTools
from allday_asr.v3.domain.recall_evidence_selection import select_evidence
from allday_asr.v3.domain.memory_content import safe_memory


def _action(tool, terms=None, targets=None, window="target"):
    return {
        "tool": tool,
        "purpose": "核对问题中的已有来源线索",
        "targets": targets or [],
        "terms": terms or [],
        "window": window,
        "evidence_ids": [],
    }


def _payload(tools, round_number, question):
    result = tools.result
    records, selection = select_evidence(
        safe_memory(result["evidence"]),
        result["investigation_steps"],
        result.get("claims", []),
    )
    if selection["omitted_count"]:
        tools.note(
            "模型本轮最多读取 20 条/28KB 依据；已优先保留引用锚点及各来源时序边界，但未读取全部证据，可能遗漏中间变更、冲突或后续，不能确认完整经过。"
        )
    refs = []
    reached = {ref for step in result["investigation_steps"] for ref in step["hits"]}
    objects = sorted(
        tools.refs.values(),
        key=lambda r: (
            r["type"] == "evidence",
            r["ref"] not in reached,
            r.get("basis") != "observed_sender",
            not any(n.casefold() in question.casefold() for n in r.get("names", [])),
        ),
    )
    for item in objects:
        if item["type"] == "evidence":
            continue
        item = {
            k: v
            for k, v in item.items()
            if k not in {"source_account_id", "sender_account_id"}
        }
        if len(json.dumps(refs + [item], ensure_ascii=False).encode()) > 12000:
            break
        refs.append(item)
    return safe_memory(
        {
            "query_id": result["query_id"],
            "round": round_number,
            "rounds_remaining": 2 - round_number,
            "question": question,
            "local_now": datetime.fromtimestamp(
                tools.now, ZoneInfo(result["scope"]["timezone"])
            ).isoformat(),
            "scope": result["scope"],
            "hard_scope": tools.root_scope,
            "time_unresolved": tools.plan.get("_parse_error", False),
            "person_mention": tools.plan["person_mention"],
            "records": records,
            "evidence_selection": selection,
            "objects": refs,
            "steps": result["investigation_steps"][-4:],
            "known_gaps": result["coverage"],
            "model_budget_seconds": max(
                0, min(25, int(tools.deadline - time.monotonic()))
            ),
            "evidence_complete": False,
        }
    )


def _claims(value, evidence, scope):
    result = []
    for claim in value["claims"]:
        citations = claim["citations"]
        if (
            not claim["text"].strip()
            or not citations
            or any(c not in evidence for c in citations)
        ):
            raise ValueError("unsupported investigation claim")
        # A text message proves that a statement was made, not that its content happened.
        if claim["kind"] == "source_fact" and any(
            evidence[c].get("evidence_type") != "native_metadata" for c in citations
        ):
            raise ValueError("speech cannot be promoted to source fact")
        event = claim["event_time"]
        if event["basis"] == "unknown":
            if any(event[k] for k in ("first", "last", "evidence_id", "quote")):
                raise ValueError("unknown time must remain unspecified")
        else:
            if (
                event["evidence_id"] not in citations
                or not event["quote"]
                or event["quote"] not in evidence[event["evidence_id"]]["quote"]
            ):
                raise ValueError("event time lacks a source fragment")
            first, last = (
                date.fromisoformat(event["first"]),
                date.fromisoformat(event["last"]),
            )
            if first > last or (last - first).days > 366:
                raise ValueError("invalid event time")
            if event["basis"] == "explicit":
                for stated in {first, last}:
                    if not any(
                        form in event["quote"]
                        for form in (
                            stated.isoformat(),
                            f"{stated.year}年{stated.month}月{stated.day}日",
                            f"{stated.year}/{stated.month}/{stated.day}",
                        )
                    ):
                        raise ValueError(
                            "full explicit date absent from source; use inferred basis"
                        )
            # Relative-day semantics use the message LOCAL date, never query/import time.
            if event["basis"] == "relative":
                from allday_asr.v3.domain.recall_query import dates

                row = evidence[event["evidence_id"]]
                period, _ = dates(event["quote"], scope["timezone"], row["sent_at"])
                if not period:
                    raise ValueError(
                        "relative time needs supported source wording or inferred basis"
                    )
                zone = ZoneInfo(scope["timezone"])
                if (first, last) != tuple(
                    datetime.fromtimestamp(t, zone).date() for t in period
                ):
                    raise ValueError("relative event date disagrees with message time")
        result.append(claim)
    return result


def _candidates(tools, value, evidence):
    candidates = []
    mention = tools.plan["person_mention"]
    for proposal in value["candidates"]:
        contact = tools.refs.get("contact:" + proposal["contact_key"])
        cited = proposal["evidence_ids"]
        if (
            not contact
            or not mention
            or not cited
            or any(cid not in evidence for cid in cited)
        ):
            raise ValueError("ungrounded address candidate")
        rows = [evidence[cid] for cid in cited]
        if not any(mention.casefold() in r["quote"].casefold() for r in rows):
            raise ValueError("address absent from evidence")
        if not any(
            r.get("source") == contact["platform"]
            and r.get("source_account_id") == contact["source_account_id"]
            and r.get("conversation_id") == contact["conversation_id"]
            and r.get("sender_account_id") == contact.get("sender_account_id")
            and r.get("source_is_from_self") is False
            for r in rows
        ):
            raise ValueError("candidate account not observed in cited context")
        candidates.append(
            {
                **contact,
                "query_local_address": True,
                "basis": "query_inference_requires_selection",
                "evidence_ids": cited,
                "explanation": proposal["explanation"],
            }
        )
    if candidates:
        tools.result.update(
            contact_candidates=candidates,
            contact_lookup_allowed=True,
            status="needs_clarification",
            model_status="insufficient",
            claims=[],
            answer="找到可能与该称呼有关的来源账号。称呼也可能指第三人，请选择本次查询对象；不会保存别名或合并人物。",
        )
        # Reuse choose(), but do not run the investigation again after explicit selection.
        tools.result["_plan"]["agentic"] = False
        tools.result["_plan"]["issue"] = ""
        return True
    return False


def investigate(service, result, plan, question, cancel, deadline, now):
    tools = InvestigationTools(service, result, plan, cancel, deadline, now)
    result.update(
        investigation_steps=[],
        investigation_version=1,
        claims=[],
        answer="现有证据不足以回答；本轮调查尚未形成可靠结论。",
    )
    result["coverage"].append(
        "调查最多两轮模型决策、八项只读检索；推断仅本次有效。消息发送时间与事件时间分别核对。"
    )
    unresolved_person = bool(
        plan["issue"] and plan["person_mention"] and not result["scope"]["person_id"]
    )
    try:
        if not plan.get("_parse_error"):
            contacts = result["scope"].get("source_contacts", [])
            terms = (
                [plan["person_mention"]]
                if unresolved_person
                else ([] if contacts else plan["terms"][:2])
            )
            targets = ["contact:" + c["key"] for c in contacts[:2]]
            if terms or targets:
                hits = tools.run(
                    _action(
                        "search_messages",
                        terms,
                        targets,
                        "identity" if unresolved_person else "target",
                    ),
                    [],
                )
                if unresolved_person and hits:
                    tools.run(
                        _action(
                            "get_message_context", targets=hits[:2], window="identity"
                        ),
                        [],
                    )
        for round_number in (1, 2):
            tools.checkpoint()
            payload = _payload(tools, round_number, question)
            if (
                payload["model_budget_seconds"] < 2
                or result["metrics"]["model_calls"] >= 2
            ):
                raise ChatDataError("MODEL_BUDGET_REACHED", 429)
            result["metrics"]["model_calls"] += 1
            result["metrics"]["model_input_ids"] = [r["id"] for r in payload["records"]]
            start = time.monotonic()
            try:
                value = checked(service.investigator(payload, cancel))
            finally:
                result["metrics"]["model_elapsed_ms"] += round(
                    (time.monotonic() - start) * 1000
                )
            tools.checkpoint()
            result["scope"] = date_scope(
                value["time"],
                result["scope"],
                result["_request"],
                now,
                plan.get("_parse_error", False),
            )
            plan["_parse_error"] = False
            result["_plan"]["scope"] = deepcopy(result["scope"])
            result["_plan"]["_parse_error"] = False
            evidence = {r["id"]: r for r in payload["records"]}
            invalid = service.sources.changed(list(evidence.values()))
            if invalid:
                raise ValueError("evidence changed during model decision")
            if _candidates(tools, value, evidence):
                break
            claims = _claims(value, evidence, result["scope"])
            if result["investigation_steps"]:
                last_step = result["investigation_steps"][-1]
                last_step["continue"] = value["mode"] == "search"
                last_step["sufficient"] = (
                    bool(claims)
                    and value["mode"] == "answer"
                    and payload["evidence_selection"]["complete_conclusion_allowed"]
                )
                last_step["assessment_basis"] = (
                    "model_decision_with_validated_citations_not_entailment_proof"
                )
            if unresolved_person and claims:
                raise ValueError("unresolved address cannot produce person conclusions")
            result["claims"] = claims
            result["unknown"] = value["unknown"]
            if claims:
                result["model_status"] = "available"
                result["answer"] = "\n".join(
                    {
                        "source_fact": "来源事实：",
                        "statement": "来源陈述：",
                        "inference": "有依据的推测：",
                    }[c["kind"]]
                    + c["text"]
                    for c in claims
                )
            if payload["evidence_selection"]["omitted_count"]:
                result["status"] = "partial"
                result["unknown"].append(
                    "部分已取得证据未送入模型，无法确认完整经过或最终状态；中间变更和矛盾仍可能遗漏。"
                )
                if claims:
                    result["answer"] = "仅据本轮选入的部分证据：\n" + result["answer"]
            if value["mode"] == "clarify":
                result.update(
                    status="needs_clarification",
                    model_status="insufficient",
                    claims=[],
                    answer="；".join(value["unknown"])
                    or "现有线索不足，请补充人物或具体事件信息。",
                )
                break
            if value["mode"] == "answer":
                if not claims:
                    result["model_status"] = "insufficient"
                    result["answer"] = "证据不足，暂时无法确认。" + "；".join(
                        value["unknown"]
                    )
                break
            outputs = []
            for action in value["actions"]:
                outputs.append(tools.run(action, outputs))
            if round_number == 2:
                tools.note(
                    "两次模型调用预算已用完；最后取回的线索尚未归纳，当前结论只覆盖已核对证据。"
                )
                result["unknown"].append("仍需进一步核对，不能确认完整经过或最终结果。")
        if result.get("model_status") == "not_requested":
            result["model_status"] = "insufficient"
    except (ChatDataError, TimeoutError) as exc:
        if cancel.is_set():
            result.update(status="cancelled", claims=[], evidence=[])
            return result
        tools.note(
            "达到预算或来源暂不可用："
            + getattr(exc, "code", "MODEL_TIMEOUT")
            + "；仅保留已核对的部分结果。"
        )
        if not result["claims"]:
            result["model_status"] = "insufficient"
        result["status"] = "partial"
    except Exception:
        result.update(
            claims=[],
            model_status="failed",
            status="partial",
            answer="调查计划或证据校验未通过，未形成可靠结论；可查看本次取得的原文。",
        )
        result["unknown"] = ["计划、身份、事件时间或引用未能通过程序校验。"]
    finally:
        invalid = service.sources.changed(result["evidence"])
        if invalid:
            service._invalidate(result, invalid)
        zone = ZoneInfo(result["scope"]["timezone"])
        for name in ("sent_from", "sent_to"):
            result["scope"][name + "_local"] = datetime.fromtimestamp(
                result["scope"][name], zone
            ).isoformat()
        result["metrics"]["hits"] = sum(
            r["role"] == "match" for r in result["evidence"]
        )
        result["metrics"]["retrieved"] = len(result["evidence"])
        result["summary_retry_available"] = False
        result["investigation_steps"] = deepcopy(result["investigation_steps"])
    return result
