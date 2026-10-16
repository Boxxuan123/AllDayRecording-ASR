"""Bounded evidence investigation, sharing HistoryRecall's lifecycle and limits."""

from copy import deepcopy
from datetime import date, datetime
from zoneinfo import ZoneInfo
import json
import re
import time
from allday_asr.v3.domain.recall_investigation import checked, date_scope
from allday_asr.v3.ports.chat_data import ChatDataError
from .recall_investigation_tools import InvestigationTools
from .recall_citation_handles import CitationHandles
from .recall_investigation_budget import normalize_scope
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


from .recall_investigation_input import payload as _payload


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
            if re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}月\d{1,2}", claim["text"]):
                raise ValueError("dated assertion cannot use unknown event time")
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
            if event["basis"] == "message":
                row = evidence[event["evidence_id"]]
                day = datetime.fromtimestamp(row["sent_at"], ZoneInfo(scope["timezone"])).date()
                hour = datetime.fromtimestamp(row["sent_at"], ZoneInfo(scope["timezone"])).hour
                from allday_asr.v3.domain.recall_event_time import validate_message_dayparts
                validate_message_dayparts(claim["text"],hour)
                if (first,last) != (day,day):
                    raise ValueError("message date disagrees with cited utterance")
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
                from allday_asr.v3.domain.recall_event_time import (
                    source_relative_period,
                )

                row = evidence[event["evidence_id"]]
                period = source_relative_period(
                    event["quote"], scope["timezone"], row["sent_at"]
                )
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
        "调查最多四次模型调用、十八次来源请求（正文最多十二次/200条），保留最终归纳额度；推断仅本次有效。消息发送时间与事件时间分别核对。"
    )
    unresolved_person = bool(
        plan["issue"] and plan["person_mention"] and not result["scope"]["person_id"]
    )
    try:
        from .recall_investigation_seed import seed_topic, seed_person
        seeded = (seed_topic(tools, _action) or seed_person(tools, _action)) if not plan.get("_parse_error") else False
        if not plan.get("_parse_error") and not seeded:
            contacts = result["scope"].get("source_contacts", [])
            identity_lookup = unresolved_person or bool(plan.get("_contact_scope_is_seed") and re.search(r"称呼|外号|对应.{0,8}账号|查清", question))
            if identity_lookup:
                contacts = []
            terms = (
                [plan["person_mention"]]
                if identity_lookup
                else ([] if contacts else tools.anchors[:1] or plan["terms"][:2])
            )
            targets = ["contact:" + c["key"] for c in contacts[:2]]
            if terms or targets:
                hits = tools.run(
                    _action(
                        "search_messages",
                        terms,
                        targets,
                        "identity" if identity_lookup else "target",
                    ),
                    [],
                )
                if identity_lookup and hits:
                    direct = [
                        h
                        for h in hits
                        if tools.refs[h].get("conversation_type") == "c2c"
                        and tools.refs[h].get("source_is_from_self") is True
                    ]
                    hits = direct or hits
                    tools.run(
                        _action(
                            "get_message_context", targets=hits[:2], window="identity"
                        ),
                        [],
                    )
        if unresolved_person:
            from allday_asr.v3.domain.recall_address import direct_address

            resolved = direct_address(
                result["evidence"], plan["person_mention"], tools.directory
            )
            if resolved:
                from .recall_understanding import apply_contact

                contact, basis = resolved
                result["identity_evidence"] = deepcopy(basis)
                result["target_resolution"] = {
                    "basis": "direct_address_with_peer_reply",
                    "mention": plan["person_mention"],
                    "target": contact["display_name"],
                    "citations": [r["id"] for r in basis],
                }
                apply_contact(plan, contact, seed=True)
                result["scope"] = plan["scope"]
                result["_plan"] = deepcopy(plan)
                result["evidence"] = []
                result["_evidence_scopes"] = {}
                ref = tools.ref("contact", contact, contact["key"])
                tools.run(_action("search_messages", targets=[ref]), [])
                if not result["evidence"]:
                    from allday_asr.v3.domain.recall_temporal import (
                        nearby_scope,
                        time_notice,
                    )

                    adjacent = nearby_scope(result["scope"], result["_request"])
                    if adjacent:
                        requested = deepcopy(result["scope"])
                        tools.run(
                            _action("search_messages", targets=[ref], window="nearby"),
                            [],
                        )
                        if result["evidence"]:
                            result["scope"].update(adjacent)
                            result["requested_scope"] = requested
                            result["time_notice"] = time_notice(
                                requested, result["scope"]
                            )
                            result["coverage"].append(result["time_notice"])
                        else:
                            result["scope"].update(requested)
                if result["evidence"]:
                    normalize_scope(result["scope"])
                    service._summarize(result, question, cancel, deadline)
                else:
                    result.update(
                        status="not_found",
                        model_status="insufficient",
                        answer=f"已根据私聊中的直接称呼与对方回复定位到{contact['display_name']}；本次指定日期未取回交流记录。称呼依据来自其他日期，未当作该日聊天。",
                    )
                return result
        for round_number in range(1, 5):
            tools.checkpoint()
            if round_number == 2:
                from .recall_investigation_followup import complete_followup

                complete_followup(tools)
            from .recall_investigation_context import complete_context
            complete_context(tools)
            payload = _payload(tools, round_number, question)
            if (
                payload["model_budget_seconds"] < 2
                or result["metrics"]["model_calls"] >= 4
            ):
                raise ChatDataError("MODEL_BUDGET_REACHED", 429)
            result["metrics"]["model_calls"] += 1
            result["metrics"]["model_input_ids"] = list(tools.model_handles.allowed.values())
            start = time.monotonic()
            try:
                value = tools.model_handles.decode(checked(service.investigator(payload, cancel)))
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
            evidence = {r["id"]: r for r in result["evidence"] if r["id"] in tools.model_handles.allowed.values()}
            invalid = service.sources.changed(list(evidence.values()))
            if invalid:
                raise ValueError("evidence changed during model decision")
            if value["candidates"] and payload["address_resolution_required"] is False:
                result.setdefault("diagnostics",[]).append(dict(stage="candidate_validation",code="CANDIDATE_REJECTED",detail="already-observed native candidates are not unknown-address proposals"))
                value["candidates"]=[]
            if _candidates(tools, value, evidence):
                break
            claims = []
            for proposed in value["claims"]:
                try:
                    claims.extend(
                        _claims({"claims": [proposed]}, evidence, result["scope"])
                    )
                except ValueError as exc:
                    result.setdefault("diagnostics", []).append(
                        {
                            "stage": "claim_validation",
                            "code": "CLAIM_REJECTED",
                            "detail": str(exc),
                        }
                    )
            if value["claims"] and not claims:
                raise ValueError("all proposed claims failed validation")
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
                cited = {cid for c in claims for cid in c["citations"]}
                days = sorted({datetime.fromtimestamp(evidence[cid]["sent_at"], ZoneInfo(result["scope"]["timezone"])).date().isoformat() for cid in cited})
                result["supported_message_dates"] = days
                result["supported_event_dates"] = [c["event_time"] for c in claims if c["event_time"]["basis"] != "unknown"]
                requested_day = datetime.fromtimestamp(result["scope"]["sent_from"], ZoneInfo(result["scope"]["timezone"])).date().isoformat()
                if days and any(day != requested_day for day in days):
                    times = [datetime.fromtimestamp(evidence[cid]["sent_at"],ZoneInfo(result["scope"]["timezone"])) for cid in cited]
                    spans = [f"{day} {min(t for t in times if t.date().isoformat()==day):%H:%M}–{max(t for t in times if t.date().isoformat()==day):%H:%M}" for day in days]
                    result["time_notice"] = f"原问题解释为 {result['scope']['sent_from_local']} 至 {result['scope']['sent_to_local']}；引用消息实际时间为 {'；'.join(spans)}，不能把相邻时段的发言当作原时段发生的事；事件或计划时间另按原文核对。"
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
            if value["mode"] == "answer" or payload["final_round"]:
                if payload["final_round"] and value["mode"] == "search":
                    tools.note("本次模型/时间预算已用完；未执行最后的搜索计划。")
                if not claims:
                    result["model_status"] = "insufficient"
                    result["answer"] = "证据不足，暂时无法确认。" + "；".join(
                        value["unknown"]
                    )
                break
            outputs = []
            for action in value["actions"]:
                outputs.append(tools.run(action, outputs))
            if round_number == 4:
                tools.note(
                    "四次模型调用预算已用完；最后取回的线索尚未归纳，当前结论只覆盖已核对证据。"
                )
                result["unknown"].append("仍需进一步核对，不能确认完整经过或最终结果。")
        if result.get("claims") and result.get("time_notice"):
            result["answer"] = result["time_notice"] + "\n" + result["answer"]
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
    except Exception as exc:
        validation = isinstance(exc, ValueError)
        result.setdefault("diagnostics", []).append(
            {
                "stage": "investigation",
                "code": "VALIDATION_FAILED" if validation else "MODEL_EXECUTION_FAILED",
                "detail": str(exc)[:200],
            }
        )
        result.update(
            claims=[],
            model_status="failed" if validation else "unavailable",
            status="partial",
            answer="调查计划或证据校验未通过，未形成可靠结论；可查看本次取得的原文。"
            if validation
            else "调查模型执行中断，暂未形成内容结论；已取回原文保留，可查看技术详情。",
        )
        result["unknown"] = [
            "计划、身份、事件时间或引用未能通过程序校验。"
            if validation
            else "模型执行失败，尚未完成内容核对。"
        ]
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
        tools.flow.value["stop_reason"] = (
            "cancelled"
            if cancel.is_set()
            else "identity_confirmation"
            if result.get("contact_candidates")
            else "validation_failed"
            if result.get("model_status") == "failed"
            else "bounded_result_with_gaps"
        )
        result["summary_retry_available"] = False
        result["investigation_steps"] = deepcopy(result["investigation_steps"])
    return result
