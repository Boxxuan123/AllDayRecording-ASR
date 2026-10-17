"""Bounded context supplements preserve source relationships, not keyword repetition."""

from allday_asr.v3.ports.chat_data import ChatDataError
from allday_asr.v3.domain.recall_event_anchor import conflicting_identifier
from allday_asr.v3.domain.recall_investigation_scope import inside_root


class InvestigationContext:
    def remote_context(self, target, scope):
        try:
            return self.remote("/v1/context", {
                "record_id": target["record_id"], "before": 8, "after": 16,
            }), None
        except ChatDataError as exc:
            left = self.remaining()
            if (exc.code != "MAC_OFFLINE" or left["source_requests"] <= 2
                    or left["body_requests"] <= 1 or left["remote_records"] < 25
                    or left["seconds"] < 30):
                raise
            # A locator timeout is not evidence invalidation. Use the existing
            # indexed message API once, in the observed native room and existing
            # 30-minute context bound, intersected with immutable user limits.
            window = {**scope,
                      "sent_from": max(scope["sent_from"], target["sent_at"] - 1800),
                      "sent_to": min(scope["sent_to"], target["sent_at"] + 1800)}
            native = {"platform": target["source"],
                      **{k: target[k] for k in ("source_account_id", "conversation_id")},
                      **{k: window[k] for k in ("sent_from", "sent_to")}}
            self.note("上下文定位超时，尝试同一授权会话的限量邻近消息；尚不表示已取得完整上下文。")
            return self.remote("/v1/messages", scope=native, limit=25), window

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
                    part, fallback_window = self.remote_context(target, selected)
                    records = part.get("items")
                    if not isinstance(records, list) or len(records) > 25:
                        raise ChatDataError("INVALID_CHAT_CONTEXT", 502)
                    self.result["metrics"]["source_records_read"] += len(records)
                    for r in records:
                        self.validate_native_record(r)
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
                    if not anchor and fallback_window is not None:
                        raise ChatDataError("CONTEXT_ANCHOR_NOT_IN_BOUNDED_PAGE", 503)
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
                        self.add(self.context_rows(rows, target, fallback_window or scope, terms))
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
    from allday_asr.v3.domain.recall_evidence_blocks import blocks
    from allday_asr.v3.domain.recall_temporal import nearby_scope

    requested = tools.result["scope"]
    first = (nearby_scope(requested, tools.result["_request"]) or requested)[
        "sent_from"
    ]
    done = tools.result.setdefault("_auto_context_rooms", [])
    rows = [
        r
        for r in tools.result["evidence"]
        if first <= (r.get("sent_at") or -1) <= requested["sent_to"]
    ]
    groups = [
        (key, group)
        for key, group in blocks(rows)
        if list(key) not in done and any(r.get("quote", "").strip() for r in group)
    ]
    from allday_asr.v3.domain.recall_episode_ranking import features, text_score

    terms = features(
        {
            "question": tools.result["_request"]["question"],
            "person_mention": tools.plan.get("person_mention", ""),
        }
    )
    related = {
        i
        for step in tools.result.get("investigation_steps", [])
        if step.get("tool") == "get_related_conversations"
        and step.get("window") != "discovery"
        for i in step.get("record_refs", step.get("hits", []))
    }
    narrative = tools.plan.get("intent") in {"timeline", "latest"}
    groups.sort(
        key=lambda p: (
            -max(text_score(r.get("quote", ""), terms) for r in p[1]),
            not any(r["id"] in related for r in p[1]),
            False if narrative else len(p[1]) >= 3,
            -max(r["sent_at"] for r in p[1]),
            repr(p[0]),
        )
    )
    # One prolific observed sender must not consume both context slots across
    # different rooms while another independently reached branch is unread.
    diverse, deferred, covered = [], [], set()
    for pair in groups:
        branches = {
            tuple(
                r.get(k) for k in ("source", "source_account_id", "sender_account_id")
            )
            for r in pair[1]
            if r["id"] in related and r.get("sender_account_id")
        }
        if branches and branches <= covered:
            deferred.append(pair)
        else:
            diverse.append(pair)
            covered.update(branches)
    groups = diverse + deferred
    # Cover distinct rooms first. A single short utterance may be the answer to
    # another participant's question and deserves context without a native reply.
    for key, group in groups:
        if list(key) in done:
            continue
        if (
            tools.result.get("_auto_context_calls", 0) >= 2
            or tools.remaining()["source_requests"] <= 1
            or tools.remaining()["body_requests"] < 1
        ):
            break
        text = [r for r in group if r.get("quote", "").strip()]
        score = max(text_score(r["quote"], terms) for r in text)
        anchors = sorted(
            [r for r in text if text_score(r["quote"], terms) == score],
            key=lambda r: (r["sent_at"], r["id"]),
        )
        # The verified API returns 8 before / 16 after. For narrative episodes,
        # anchor inside the fragment instead of repeatedly walking its tail.
        target = anchors[(len(anchors) - 1) // 3] if narrative else anchors[-1]
        tools.run(
            {
                "tool": "get_message_context",
                "purpose": "补齐新会话的提问、简短回答、否定与后续；相邻不等于同一事件",
                "targets": [target["id"]],
                "terms": [],
                "window": "episode",
                "evidence_ids": [target["id"]],
            },
            [],
        )
        tools.result["_auto_context_calls"] = (
            tools.result.get("_auto_context_calls", 0) + 1
        )
        done.append(list(key))
