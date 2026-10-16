"""Bounded context supplements preserve source relationships, not keyword repetition."""

from allday_asr.v3.ports.chat_data import ChatDataError
from allday_asr.v3.domain.recall_event_anchor import conflicting_identifier
from allday_asr.v3.domain.recall_investigation_scope import inside_root


class InvestigationContext:
    def context(self, targets, scope, terms=()):
        found = []
        for target in targets:
            if target["type"] != "evidence" or target["source"] == "recording":
                continue
            if not inside_root(target, self.root_scope, evidence=True):
                self.note("上下文目标不属于原查询硬范围，未读取。")
                continue
            selected = self.bounded_scope({**scope, "relationship": "topic"})
            if selected is None:
                continue
            self.current_scope = selected
            try:
                rows = self.service.sources.context(target, selected)
            except ChatDataError:
                rows = []
            rows = self.cache_rows(rows)
            self.flow.received(rows, "cache", "bounded_context")
            anchor = next(
                (r for r in rows if r["record_id"] == target["record_id"]), None
            )
            if anchor and anchor["revision"] == target["revision"]:
                found.extend(self.add(self.context_rows(rows, target, scope, terms)))
            if self.result["_request"].get("expand", True):
                try:
                    self.verify_remote()
                    if self.result["metrics"]["source_records_read"] + 25 > 200:
                        raise ChatDataError("SOURCE_REQUEST_BUDGET_REACHED", 429)
                    part = self.remote(
                        "/v1/context",
                        {"record_id": target["record_id"], "before": 8, "after": 16},
                    )
                    records = part.get("items")
                    if not isinstance(records, list) or len(records) > 25:
                        raise ChatDataError("INVALID_CHAT_CONTEXT", 502)
                    self.result["metrics"]["source_records_read"] += len(records)
                    for r in records:
                        self.service.sources.validate_record(r)
                        if (
                            r["platform"],
                            r["source_account_id"],
                            r["conversation_id"],
                        ) != (
                            target["source"],
                            target["source_account_id"],
                            target["conversation_id"],
                        ):
                            self.flow.record(
                                "hard_rejected", r, "context_identity_mismatch"
                            )
                            raise ChatDataError("CONTEXT_IDENTITY_MISMATCH", 502)
                    anchor = next(
                        (r for r in records if r["record_id"] == target["record_id"]),
                        None,
                    )
                    if not anchor or anchor["record_revision"] > target["revision"]:
                        self.service.sources.mark_changed(target, "SOURCE_CHANGED")
                        raise ChatDataError("SOURCE_CHANGED", 409)
                    if anchor["record_revision"] < target["revision"]:
                        raise ChatDataError("SOURCE_REVISION_BEHIND", 503)
                    rows = [
                        self.service.sources.evidence(r, "mac", "context")
                        for r in records
                    ]
                    found.extend(
                        self.add(self.context_rows(rows, target, scope, terms))
                    )
                except ChatDataError as exc:
                    self.note("上下文未完整核对：" + exc.code)
        return list(dict.fromkeys(found))

    def context_rows(self, rows, target, scope, terms):
        kept = []
        for row in rows:
            self.flow.record("candidate_seen", row, "bounded_context")
            same = all(
                row.get(k) == target.get(k)
                for k in ("source", "source_account_id", "conversation_id")
            )
            if (
                not same
                or not scope["sent_from"]
                <= (row.get("sent_at") or -1)
                <= scope["sent_to"]
            ):
                self.flow.record("hard_rejected", row, "context_scope_or_date")
                continue
            linked = (
                row.get("reply_record_id") == target["record_id"]
                or target.get("reply_record_id") == row["record_id"]
            )
            linked = (
                linked
                or row.get("quote_record_id") == target["record_id"]
                or target.get("quote_record_id") == row["record_id"]
            )
            if terms and not linked and conflicting_identifier(row["quote"], terms):
                self.flow.record(
                    "soft_rejected", row, "different_context_event_identifier"
                )
                continue
            adjacent = (
                abs((row.get("sent_at") or 0) - (target.get("sent_at") or 0)) <= 1800
            )
            if terms and not (
                linked
                or adjacent
                or all(t.casefold() in row["quote"].casefold() for t in terms)
            ):
                self.flow.record("soft_rejected", row, "no_bounded_context_relation")
                continue
            kept.append(
                {
                    **row,
                    "retrieval_basis": "reply_reference"
                    if linked
                    else "bounded_conversation_context",
                    "relation_ids": [target["id"]],
                    "relevance_confirmed": False,
                }
            )
        return kept



def complete_context(tools):
    """Supplement newly reached continuous actor speech with other participants."""
    from allday_asr.v3.domain.recall_evidence_blocks import blocks, conversation
    from allday_asr.v3.domain.recall_temporal import nearby_scope
    requested = tools.result["scope"]
    first = (nearby_scope(requested,tools.result["_request"]) or requested)["sent_from"]
    done = tools.result.setdefault("_auto_context_rooms", [])
    rows = [r for r in tools.result["evidence"] if first <= (r.get("sent_at") or -1) <= requested["sent_to"]]
    groups = [(key,group) for key,group in blocks(rows)
        if list(key) not in done and sum(bool(r.get("quote", "").strip()) for r in group)>=3]
    groups.sort(key=lambda p:-sum(bool(r.get("quote", "").strip()) for r in p[1]))
    for key,group in groups:
        remaining = 2 - tools.result.get("_auto_context_calls",0)
        if not remaining or tools.remaining()["source_requests"]<=1 or tools.remaining()["body_requests"]<1:
            break
        # Quantiles cover the beginning and later explanation of long fragments;
        # no content word, sender name or source ID contributes a score.
        text = [r for r in group if r.get("quote", "").strip()]
        indices = [len(text)//4,3*len(text)//4] if len(text)>=10 and remaining>=2 else [len(text)//2]
        for index in dict.fromkeys(indices):
            target = text[min(index,len(text)-1)]
            tools.run({"tool":"get_message_context","purpose":"补齐新发现连续片段中的其他参与者问答、否定和后续；相邻不等于同一事件",
                "targets":[target["id"]],"terms":[],"window":"episode","evidence_ids":[target["id"]]},[])
            tools.result["_auto_context_calls"] = tools.result.get("_auto_context_calls",0)+1
        done.append(list(key))
