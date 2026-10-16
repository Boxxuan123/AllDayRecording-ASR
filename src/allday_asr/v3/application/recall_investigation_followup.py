"""Deterministic bounded source traversal before the existing final model call."""

from allday_asr.v3.domain.recall_event_anchor import literal_anchor
from allday_asr.v3.ports.chat_data import ChatDataError


def action(tool, targets, anchor, support):
    return {
        "tool": tool,
        "purpose": "沿已取得原文的稳定账号及字面事件锚点补取后续；候选相关性仍待核对",
        "targets": targets[:4],
        "terms": [anchor],
        "window": "followup",
        "evidence_ids": support[:6],
    }


def complete_followup(tools):
    if not tools.anchors:
        return
    if (
        tools.calls >= 13
        or tools.remaining()["source_requests"] <= 2
        or tools.remaining()["body_requests"] <= 1
    ):
        tools.flow.gap("followup_action_budget")
        tools.note("剩余调查动作不足以完成账号、事件和上下文追踪；后续证据未完整取得。")
        return
    scope = tools.scope("followup")
    if scope is None:
        tools.flow.gap("followup_hard_scope")
        return
    if scope["sent_to"] <= tools.result["scope"]["sent_to"]:
        tools.flow.gap("followup_date_limited")
        tools.note("后续日期受用户硬范围或当前时间限制，无法据此确认更晚进展。")
    records = sorted(
        tools.result["evidence"], key=lambda r: r.get("sent_at") or 0, reverse=True
    )
    anchor = next(
        (
            a
            for a in tools.anchors
            if any(literal_anchor(r["quote"], [a]) for r in records)
        ),
        None,
    )
    if not anchor:
        tools.flow.gap("no_literal_event_anchor")
        return
    accounts, searched = set(), set()
    seeds = []
    try:
        # Reserve one action for context; every hop still uses the shared counters.
        hops = 0
        while (
            tools.calls < 13
            and hops < 2
            and tools.remaining()["source_requests"] > 2
            and tools.remaining()["body_requests"] > 1
        ):
            hops += 1
            records = sorted(
                tools.result["evidence"],
                key=lambda r: r.get("sent_at") or 0,
                reverse=True,
            )
            seeds = []
            for row in records:
                key = (
                    row["source"],
                    row.get("source_account_id"),
                    row.get("sender_account_id"),
                )
                if (
                    not literal_anchor(row["quote"], [anchor])
                    or not key[2]
                    or row.get("source_is_from_self") is not False
                    or key in accounts
                ):
                    continue
                seeds.append(row["id"])
                accounts.add(key)
                if len(seeds) == 4:
                    break
            if not seeds:
                break
            contacts = tools.run(
                action("get_related_conversations", seeds, anchor, seeds), []
            )
            visited = {
                (r["source"], r.get("source_account_id"), r.get("conversation_id"))
                for r in records
            }
            contacts = [c for c in dict.fromkeys(contacts) if c not in searched]
            contacts.sort(
                key=lambda ref: (
                    tuple(
                        tools.refs[ref].get(k)
                        for k in ("platform", "source_account_id", "conversation_id")
                    )
                    in visited,
                    ref,
                )
            )
            # Existing conversations have already supplied their bridge. Spend the
            # limited remote reads on new rooms before revisiting any old room.
            new = [
                c
                for c in contacts
                if tuple(
                    tools.refs[c].get(k)
                    for k in ("platform", "source_account_id", "conversation_id")
                )
                not in visited
            ]
            contacts = (new or contacts)[:4]
            if not contacts:
                tools.flow.gap("no_authorized_related_conversation")
                break
            tools.run(action("search_messages", contacts, anchor, seeds), [])
            searched.update(contacts)
        # Preserve a bounded adjacent reply/explanation for each matched conversation.
        targets, seen = [], set()
        for row in sorted(
            tools.result["evidence"], key=lambda r: r.get("sent_at") or 0, reverse=True
        ):
            key = (
                row["source"],
                row.get("source_account_id"),
                row.get("conversation_id"),
            )
            if key not in seen and literal_anchor(row["quote"], [anchor]):
                targets.append(row["id"])
                seen.add(key)
            if len(targets) == 4:
                break
        if targets and tools.calls < 16 and tools.remaining()["source_requests"] > 0:
            tools.run(action("get_message_context", targets, anchor, seeds), [])
        tools.flow.gap("followup_bounded_not_exhaustive")
    except (ChatDataError, TimeoutError) as exc:
        tools.flow.gap(getattr(exc, "code", "followup_timeout"))
        tools.note("后续关系追踪未完成，仍缺跨会话或较晚的原始依据。")
