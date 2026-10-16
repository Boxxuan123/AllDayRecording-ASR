"""Capability-limited reads used by the existing HistoryRecall lifecycle."""

from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo
import json
import hashlib
import time
from allday_asr.v3.domain.recall_investigation import window_scope
from allday_asr.v3.ports.chat_data import ChatDataError
from allday_asr.v3.domain.recall_investigation_scope import (
    remote_grants,
    contact_record_allowed,
    intersect_root,
    inside_root,
)


from .recall_investigation_context import InvestigationContext
from .recall_evidence_flow import EvidenceFlow
from allday_asr.v3.domain.recall_event_anchor import event_anchors, literal_anchor
from .recall_investigation_relevance import InvestigationRelevance


READ_TOOLS = frozenset(
    {
        "find_contacts",
        "find_conversations",
        "search_messages",
        "get_group_members",
        "get_related_conversations",
        "get_message_context",
        "get_existing_memory",
        "get_source_evidence",
    }
)


from .recall_investigation_budget import InvestigationBudget


class InvestigationTools(InvestigationContext, InvestigationRelevance, InvestigationBudget):
    def __init__(self, service, result, plan, cancel, deadline, now):
        self.service, self.result, self.plan = service, result, plan
        self.cancel, self.deadline, self.now = cancel, deadline, now
        self.refs, self.seen, self.remote_checked = {}, set(), False
        self.root_scope = deepcopy(result["scope"])
        # Automatically resolved directory seeds are leads, not an explicit user filter.
        if plan.get("_contact_scope_is_seed"):
            self.root_scope.pop("source_contacts", None)
            question = result["_request"]["question"]
            import re

            if not re.search(r"QQ|微信|录音", question, re.I):
                self.root_scope["source"] = result["_request"].get("source", "all")
        self.root_scope["sent_from"] = result["_request"].get("sent_from", 0)
        self.root_scope["sent_to"] = min(now, result["_request"].get("sent_to", now))
        self.normalize_scopes()
        self.authorized_directory = deepcopy(plan.get("_directory", []))
        from allday_asr.v3.domain.recall_query import name_in_text

        self.directory = sorted(
            [c for c in plan.get("_directory", []) if inside_root(c, self.root_scope)],
            key=lambda c: (
                not any(
                    name_in_text(n, result["_request"]["question"])
                    or (
                        c["conversation_type"] == "group"
                        and any(
                            t.casefold() in n.casefold() for t in plan.get("terms", [])
                        )
                    )
                    for n in c["names"]
                )
            ),
        )
        self.current_window = "target"
        self.calls = 0
        self.flow = EvidenceFlow(result)
        self.anchors = event_anchors(
            result["_request"]["question"], plan.get("person_mention", "")
        )
        self.current_scope = result["scope"]
        result["_evidence_scopes"] = {}
        for contact in self.directory[:500]:
            self.ref("contact", contact, contact["key"])

    def ref(self, kind, value, key):
        ref = kind + ":" + key
        self.refs[ref] = {"ref": ref, "type": kind, **value}
        return ref

    def checkpoint(self):
        self.service._checkpoint(self.cancel, self.deadline)

    def note(self, message):
        if message not in self.result["coverage"]:
            self.result["coverage"].append(message)

    def bounded_scope(self, scope):
        bounded = intersect_root(self.root_scope, scope)
        if bounded is None:
            self.note("调查目标与原查询硬筛选不相交，未读取该范围；当前覆盖不足。")
        return bounded

    def add(self, rows):
        self.checkpoint()
        allowed = []
        for row in rows:
            if inside_root(row, self.root_scope, evidence=True):
                allowed.append(row)
            else:
                self.flow.record("hard_rejected", row, "immutable_scope")
        if len(allowed) != len(rows):
            self.note("已排除原查询硬范围以外的证据。")
        rows = allowed
        invalid = self.service.sources.changed(rows)
        saved = {r["id"]: r for r in self.result["evidence"]}
        refs = []
        for row in rows:
            if row["id"] in invalid:
                self.flow.record("hard_rejected", row, "source_changed")
                self.note("部分来源已修订或撤回，已排除旧证据。")
                continue
            self.flow.record(
                "application_accepted", row, "hard_scope_and_integrity_valid"
            )
            old = saved.get(row["id"])
            if old and old["revision"] > row["revision"]:
                continue
            if len(saved) >= 300 and row["id"] not in saved:
                self.note("已达到 300 条证据上限，剩余线索未覆盖。")
                break
            if old and row["revision"] > old["revision"] and self.result["claims"]:
                self.result.update(
                    claims=[],
                    model_status="stale",
                    answer="来源在调查期间更新，旧结论已失效，请重新查询。",
                )
            self.flow.record(
                "candidate",
                row,
                row.get("retrieval_basis", "scoped_match"),
                row.get("relation_ids", []),
            )
            saved[row["id"]] = row
            context_scope = deepcopy(self.current_scope)
            context_scope.pop("source_contacts", None)
            context_scope.pop("source_accounts", None)
            context_scope["relationship"] = "topic"
            context_scope = self.bounded_scope(context_scope)
            self.result["_evidence_scopes"][row["id"]] = context_scope
            self.refs[row["id"]] = {"ref": row["id"], "type": "evidence", **row}
            refs.append(row["id"])
            if (
                row["source"] != "recording"
                and row.get("sender_account_id")
                and row.get("source_is_from_self") is False
            ):
                stable = [
                    row.get("dataset"),
                    row["source"],
                    row["source_account_id"],
                    row["conversation_id"],
                    row["sender_account_id"],
                ]
                key = hashlib.sha256(
                    json.dumps(stable, ensure_ascii=False).encode()
                ).hexdigest()[:32]
                contact = {
                    "key": key,
                    "platform": row["source"],
                    "source_account_id": row["source_account_id"],
                    "conversation_id": row["conversation_id"],
                    "sender_account_id": row["sender_account_id"],
                    "conversation_type": row["conversation_type"],
                    "display_name": row["speaker"],
                    "names": [row["speaker"]],
                    "dataset": row.get("dataset"),
                    "basis": "observed_sender",
                    "evidence_ids": [row["id"]],
                }
                self.ref("contact", contact, key)
        self.result["evidence"] = list(saved.values())
        return refs

    def resolve(self, action, outputs):
        targets = []
        for name in action["targets"]:
            if name.startswith("$"):
                try:
                    index = int(name[1:])
                    if index < 0 or index >= len(outputs):
                        raise ValueError()
                    targets.extend(outputs[index])
                except (ValueError, IndexError):
                    raise ValueError("invalid dependent target") from None
            else:
                targets.append(name)
        if any(r not in self.refs for r in targets):
            raise ValueError("unknown investigation target")
        if len(targets) > 4:
            self.note("关系分支最多展开两个来源对象，其余分支尚未覆盖。")
        values = [self.refs[r] for r in dict.fromkeys(targets)]
        if action["tool"] == "get_related_conversations":
            seen = getattr(self, "related_actors", set())
            def actor(r):
                return (r.get("source",r.get("platform")),r.get("source_account_id"),r.get("sender_account_id"))
            values = [r for r in values if r.get("source_is_from_self") is not True]
            values.sort(key=lambda r: actor(r) in seen)
            values = list({actor(r):r for r in values}.values())
        return values[:2]

    def run(self, action, outputs):
        if action.get("tool") not in READ_TOOLS:
            raise ValueError("tool is not in the read capability allowlist")
        self.checkpoint()
        if self.calls >= 16:
            raise ChatDataError("INVESTIGATION_TOOL_BUDGET_REACHED", 429)
        targets = self.resolve(action, outputs)
        allowed = {
            "get_message_context": {"evidence"},
            "get_source_evidence": {"evidence"},
            "get_related_conversations": {"evidence", "account"},
        }.get(action["tool"])
        if allowed and any(r["type"] not in allowed for r in targets):
            raise ValueError("invalid tool target type")
        if any(
            e not in self.refs or self.refs[e]["type"] != "evidence"
            for e in action["evidence_ids"]
        ):
            raise ValueError("unknown supporting evidence")
        signature = json.dumps(
            [
                action["tool"],
                [r["ref"] for r in targets],
                action["terms"],
                action["window"],
            ],
            sort_keys=True,
        )
        if signature in self.seen:
            self.note("已停止重复检索计划；现有证据与覆盖仍有限。")
            return []
        self.seen.add(signature)
        self.calls += 1
        self.result["metrics"]["investigation_tools"] = self.calls
        scope = self.scope(action["window"])
        if scope is None:
            return []
        self.current_scope = scope
        self.current_window = action["window"]
        tool = action["tool"]
        before = {(r["id"], r.get("revision")) for r in self.result["evidence"]}
        support = list(action["evidence_ids"])
        for target in targets:
            support.extend(target.get("citations", target.get("evidence_ids", [])))
            if target["type"] == "evidence": support.append(target["ref"])
        trace = {
            "purpose": action["purpose"],
            "tool": tool,
            "targets": [r["ref"] for r in targets],
            "window": action["window"],
            "sent_from": scope["sent_from"],
            "sent_to": scope["sent_to"],
            "support": list(dict.fromkeys(support)),
            "hits": [],
            "sufficient": False,
            "continue": True,
        }
        self.result["investigation_steps"].append(trace)
        if tool in {"find_contacts", "find_conversations"}:
            refs = [
                r["ref"]
                for r in self.refs.values()
                if r["type"] == "contact"
                and (tool != "find_contacts" or r["conversation_type"] == "c2c")
                and action["terms"]
                and all(
                    any(t.casefold() in n.casefold() for n in r["names"])
                    for t in action["terms"]
                )
            ][:20]
        elif tool == "search_messages":
            refs = self.search(targets, action, scope)
        elif tool == "get_group_members":
            refs = self.members(targets, action, scope)
        elif tool == "get_related_conversations":
            refs = self.related(targets, scope, action["terms"])
        elif tool == "get_message_context":
            refs = self.context(targets, scope, action["terms"])
        elif tool == "get_existing_memory":
            if self.root_scope.get("source_contacts") or self.root_scope.get(
                "source_accounts"
            ):
                self.note("已有记忆无法证明属于当前指定会话，未读取；覆盖不足。")
                return []
            rows = self.service.sources.existing_memory(
                scope,
                action["terms"],
                self.cancel,
                min(self.deadline, time.monotonic() + 3),
            )
            refs = [self.ref("memory", r, r["memory_id"]) for r in rows]
            self.note("已有事件/人物记忆仅作为线索；最终结论仍需原始聊天或录音证据。")
        else:
            rows = [r for r in targets if r["type"] == "evidence"]
            refs = self.add(rows)
        from allday_asr.v3.domain.recall_evidence_blocks import register_step
        register_step(trace, refs, self.refs, before, self.result["evidence"])
        return refs

    def search(self, targets, action, scope):
        if not action["terms"] and not targets:
            raise ValueError("unbounded message search")
        found = []
        for target in targets or [None]:
            selected = deepcopy(scope)
            selected.pop("source_contacts", None)
            selected.pop("source_accounts", None)
            selected["relationship"] = "topic"
            if target is not None:
                if target["type"] == "evidence" and target["source"] != "recording":
                    target = {"type": "contact", "platform": target["source"], **target}
                if target["type"] not in {"contact", "account"}:
                    raise ValueError(
                        "message target must be a contact or stable account"
                    )
                if selected["source"] not in {"all", target["platform"]}:
                    raise ValueError("source constraint violation")
                selected["source"] = target["platform"]
                selected["source_accounts"] = [
                    {k: target[k] for k in ("platform", "source_account_id")}
                ]
                if target["type"] == "contact":
                    if target.get("relation_basis") and not self.branch_supported(
                        target, action
                    ):
                        raise ValueError(
                            "related conversation search needs cited actor evidence"
                        )
                    selected["source_contacts"] = [target]
                else:
                    if not self.branch_supported(target, action):
                        raise ValueError(
                            "cross-conversation search needs cited actor evidence"
                        )
                    selected["source_senders"] = [target["sender_account_id"]]
            elif self.result["scope"].get("source_contacts") and not self.plan.get("_contact_scope_is_seed"):
                selected["source_contacts"] = self.result["scope"]["source_contacts"]
            selected = self.bounded_scope(selected)
            if selected is None:
                continue
            self.current_scope = selected
            remaining = min(
                40 if targets and not action["terms"] else 20,
                300 - len(self.result["evidence"]),
            )
            if remaining <= 0:
                self.note("证据预算耗尽。")
                break
            if selected["source"] != "recording":
                try:
                    rows, more = self.service.sources.chat(
                        selected,
                        self.candidate_terms(action["terms"]),
                        remaining,
                        self.cancel,
                        min(self.deadline, time.monotonic() + 3),
                    )
                except ChatDataError as exc:
                    if exc.code not in {"CACHE_BODY_BUDGET_REACHED", "CACHE_RECORD_BUDGET_REACHED"}:
                        raise
                    rows,more=[],True
                    self.note("缓存正文额度已用完；仅在剩余来源额度内继续授权远端检索，不重置缓存计数。")
                rows = self.cache_rows(rows)
                self.flow.received(rows, "cache", "topic_search")
                found.extend(self.add(self.topic_rows(rows, action["terms"])))
                if more:
                    self.note("单次聊天检索只读取有限条记录；未覆盖其他页。")
                # Existing source API, never a new network destination or arbitrary query.
                if self.result["_request"].get("expand", True):
                    try:
                        found.extend(self.search_remote(selected, action["terms"]))
                    except ChatDataError as exc:
                        self.note(
                            "来源未完整取回：" + exc.code + "；保留部分缓存依据。"
                        )
            if selected["source"] not in {"qq", "wechat"} and target is None:
                rows, more = self.service.sources.recordings(
                    selected,
                    action["terms"],
                    remaining,
                    self.cancel,
                    min(self.deadline, time.monotonic() + 3),
                )
                self.flow.received(rows, "cache", "recording_search")
                found.extend(self.add(rows))
                if more:
                    self.note("单次录音检索只读取前 20 条。")
        return list(dict.fromkeys(found))

    def search_remote(self, scope, terms):
        scope = self.bounded_scope(scope)
        if scope is None:
            return []
        grants = remote_grants(
            scope,
            self.service.sources.authorized_scopes(scope),
            self.authorized_directory,
        )
        if len(grants) > 2:
            self.note("远端范围超过本轮两个分支上限，其余会话尚未覆盖。")
        if not grants:
            self.note("未发现可用于远端检索的已授权账号或会话。")
            return []
        self.verify_remote()
        found = []
        for grant in grants[:2]:
            remote = {
                **grant,
                "sent_from": scope["sent_from"],
                "sent_to": scope["sent_to"],
            }
            if terms:
                remote["keyword"] = self.candidate_terms(terms)[0]
            limit = min(
                15 if self.current_window == "discovery" else 40 if scope.get("source_contacts") and not terms else 20,
                200 - self.result["metrics"]["source_records_read"],
            )
            if limit <= 0:
                break
            page = self.remote("/v1/messages", scope=remote, limit=limit)
            items = page.get("items")
            if not isinstance(items, list) or len(items) > limit:
                raise ChatDataError("INVALID_CHAT_PAGE", 502)
            self.result["metrics"]["source_records_read"] += len(items)
            rows = []
            for record in items:
                self.service.sources.validate_record(record)
                if (
                    any(
                        record.get(k) != v
                        for k, v in remote.items()
                        if k
                        in {
                            "platform",
                            "source_account_id",
                            "conversation_id",
                            "sender_account_id",
                        }
                    )
                    or not contact_record_allowed(record, scope)
                    or not scope["sent_from"]
                    <= (record.get("sent_at") or -1)
                    <= scope["sent_to"]
                ):
                    self.flow.record("hard_rejected", record, "source_scope_mismatch")
                    raise ChatDataError("CONTEXT_IDENTITY_MISMATCH", 502)
                row = self.service.sources.evidence(record, "mac")
                if not scope["person_id"] or row["person_id"] == scope["person_id"]:
                    rows.extend(self.topic_rows([row], terms))
                else:
                    self.flow.record("hard_rejected", row, "person_permission")
            found.extend(self.add(rows))
            if page.get("has_more"):
                self.note("Mac 尚有后续页，本轮预算内没有完整覆盖。")
        return found

    def members(self, targets, action, scope):
        refs = []
        self.note(
            "现有来源契约没有群成员名册和群角色；这里只使用群内发言账号，不能确认成员身份、群主或现实职务。"
        )
        for target in targets:
            if target["type"] != "contact" or target["conversation_type"] != "group":
                continue
            hits = self.search([target], {**action, "terms": []}, scope)
            for hit in hits:
                row = self.refs[hit]
                if (
                    not row.get("sender_account_id")
                    or row.get("effective_is_self") is True
                ):
                    continue
                account = {
                    "platform": row["source"],
                    "source_account_id": row["source_account_id"],
                    "sender_account_id": row["sender_account_id"],
                    "display_name": row["speaker"],
                    "basis": "observed_speech_only",
                    "citations": [hit],
                    "person_id": row["person_id"],
                }
                key = json.dumps(
                    [
                        account[k]
                        for k in ("platform", "source_account_id", "sender_account_id")
                    ],
                    separators=(",", ":"),
                )
                refs.append(self.ref("account", account, key))
        return list(dict.fromkeys(refs))

    def related(self, targets, scope, terms=()):
        # A directory-resolved seed must not become a conversation restriction
        # on the account relationship scan. Explicit root filters remain binding.
        scope = deepcopy(scope)
        if self.plan.get("_contact_scope_is_seed"):
            scope.pop("source_contacts", None)
        scope = self.bounded_scope(scope)
        if scope is None:
            return []
        refs = []
        for target in targets:
            if target["type"] == "evidence" and target.get("sender_account_id"):
                target = {**target, "platform": target["source"], "type": "account"}
            if target["type"] != "account":
                continue
            if not hasattr(self,"related_actors"): self.related_actors = set()
            self.related_actors.add((target["platform"],target["source_account_id"],target["sender_account_id"]))
            if scope["person_id"] and target.get("person_id") != scope["person_id"]:
                raise ValueError("person constraint violation")
            rows, more = self.service.sources.related_conversations(
                target, scope, self.cancel, min(self.deadline, time.monotonic() + 3)
            )
            for item in rows:
                row = item["evidence"]
                self.flow.received([row], "cache", "account_relationship")
                if not inside_root(row, self.root_scope, evidence=True):
                    self.flow.record("hard_rejected", row, "relationship_scope")
                    continue
                if self.service.sources.changed([row]):
                    self.flow.record(
                        "hard_rejected", row, "relationship_source_changed"
                    )
                    continue
                if not terms or literal_anchor(row["quote"], terms):
                    if not self.add([row]):
                        continue
                else:
                    self.flow.record(
                        "relationship_lead",
                        row,
                        "same_account_not_same_event",
                        [target["ref"]],
                    )
                c = item["contact"]
                c["relation_account"] = {
                    k: target[k]
                    for k in ("platform", "source_account_id", "sender_account_id")
                }
                c["relation_evidence"] = [item["evidence"]["id"]]
                c["relation_basis"] = item.get(
                    "relation_basis", "same_scoped_account_speech"
                )
                refs.append(self.ref("contact", c, c["key"]))
            if self.result["_request"].get("expand", True) and not rows:
                try:
                    refs.extend(self.remote_related(target, scope))
                except ChatDataError as exc:
                    self.note("账号关系未完整取回：" + exc.code)
            for c in self.directory:
                if c.get("sender_account_id") and all(
                    c.get(k) == target.get(k)
                    for k in ("platform", "source_account_id", "sender_account_id")
                ):
                    refs.append(self.ref("contact", c, c["key"]))
            if more:
                self.note("账号会话关系扫描达到 100 条/20 会话上限。")
        self.note("跨会话关系仅覆盖已有缓存和原生目录；同账号参与不能证明是同一事件。")
        return list(dict.fromkeys(refs))
