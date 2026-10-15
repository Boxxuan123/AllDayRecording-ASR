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
)


class InvestigationTools:
    def __init__(self, service, result, plan, cancel, deadline, now):
        self.service, self.result, self.plan = service, result, plan
        self.cancel, self.deadline, self.now = cancel, deadline, now
        self.refs, self.seen, self.remote_checked = {}, set(), False
        self.directory = sorted(
            plan.get("_directory", []),
            key=lambda c: (
                not any(
                    n.casefold() in result["_request"]["question"].casefold()
                    for n in c["names"]
                )
            ),
        )
        self.calls = 0
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

    def add(self, rows):
        self.checkpoint()
        invalid = self.service.sources.changed(rows)
        saved = {r["id"]: r for r in self.result["evidence"]}
        refs = []
        for row in rows:
            if row["id"] in invalid:
                self.note("部分来源已修订或撤回，已排除旧证据。")
                continue
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
            saved[row["id"]] = row
            context_scope = deepcopy(self.current_scope)
            context_scope.pop("source_contacts", None)
            context_scope.pop("source_accounts", None)
            context_scope["relationship"] = "topic"
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

    def remote(self, route, body=None, **params):
        self.checkpoint()
        m = self.result["metrics"]
        if (
            m["retrieval_requests"] >= 6
            or m["source_requests"] >= 12
            or m["source_records_read"] >= 100
        ):
            raise ChatDataError("SOURCE_REQUEST_BUDGET_REACHED", 429)
        m["retrieval_requests"] += 1
        m["source_requests"] += 1
        value = self.service.source.request(route, body, **params)
        self.checkpoint()
        return value

    def verify_remote(self):
        if not self.remote_checked:
            status = self.remote("/v1/status")
            if status.get("dataset_id") != self.service.sources.dataset():
                raise ChatDataError("DATASET_MISMATCH", 409)
            if status.get("status") != "ready":
                raise ChatDataError("SOURCE_NOT_READY", 503)
            self.remote_checked = True

    def scope(self, window):
        value = window_scope(
            self.result["scope"], window, self.result["_request"], self.now
        )
        if window != "target":
            zone = self.result["scope"]["timezone"]
            self.note(
                f"{window} 线索窗口：{datetime.fromtimestamp(value['sent_from'], ZoneInfo(zone)).isoformat()} 至 {datetime.fromtimestamp(value['sent_to'], ZoneInfo(zone)).isoformat()}（Unix边界 {value['sent_from']}–{value['sent_to']}，用户时区 {zone}）；消息时间不等于事件发生时间。"
            )
        return value

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
            self.note("关系分支最多展开四个来源对象，其余分支尚未覆盖。")
        return [self.refs[r] for r in dict.fromkeys(targets)][:4]

    def run(self, action, outputs):
        self.checkpoint()
        if self.calls >= 8:
            raise ChatDataError("INVESTIGATION_TOOL_BUDGET_REACHED", 429)
        targets = self.resolve(action, outputs)
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
        self.current_scope = scope
        tool = action["tool"]
        trace = {
            "purpose": action["purpose"],
            "tool": tool,
            "targets": [r["ref"] for r in targets],
            "window": action["window"],
            "sent_from": scope["sent_from"],
            "sent_to": scope["sent_to"],
            "support": action["evidence_ids"],
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
            refs = self.related(targets, scope)
        elif tool == "get_message_context":
            refs = self.context(targets, scope)
        elif tool == "get_existing_memory":
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
        trace["hits"] = refs
        trace["relations"] = [
            {"ref": r, "type": self.refs[r]["type"]} for r in refs if r in self.refs
        ]
        trace["sufficient"] = (
            False  # Retrieval hits alone do not establish sufficiency.
        )
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
                    if target.get("relation_basis") and (
                        not action["terms"]
                        or not action["evidence_ids"]
                        or not all(
                            any(
                                t.casefold() in self.refs[e]["quote"].casefold()
                                for e in action["evidence_ids"]
                            )
                            for t in action["terms"]
                        )
                    ):
                        raise ValueError(
                            "related conversation search needs cited event anchor"
                        )
                    selected["source_contacts"] = [target]
                else:
                    if (
                        not action["terms"]
                        or not action["evidence_ids"]
                        or not all(
                            any(
                                t.casefold() in self.refs[e]["quote"].casefold()
                                for e in action["evidence_ids"]
                            )
                            for t in action["terms"]
                        )
                    ):
                        raise ValueError(
                            "cross-conversation account search needs a cited event anchor"
                        )
                    selected["source_senders"] = [target["sender_account_id"]]
            elif self.result["scope"].get("source_contacts"):
                selected["source_contacts"] = self.result["scope"]["source_contacts"]
            remaining = min(20, 300 - len(self.result["evidence"]))
            if remaining <= 0:
                self.note("证据预算耗尽。")
                break
            if selected["source"] != "recording":
                rows, more = self.service.sources.chat(
                    selected,
                    action["terms"],
                    remaining,
                    self.cancel,
                    min(self.deadline, time.monotonic() + 3),
                )
                found.extend(self.add(rows))
                if more:
                    self.note("单次聊天检索只读取前 20 条；未覆盖其他页。")
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
                found.extend(self.add(rows))
                if more:
                    self.note("单次录音检索只读取前 20 条。")
        return list(dict.fromkeys(found))

    def search_remote(self, scope, terms):
        grants = remote_grants(
            scope, self.service.sources.authorized_scopes(scope), self.directory
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
                remote["keyword"] = terms[0]
            limit = min(20, 100 - self.result["metrics"]["source_records_read"])
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
                    raise ChatDataError("CONTEXT_IDENTITY_MISMATCH", 502)
                row = self.service.sources.evidence(record, "mac")
                if (
                    not scope["person_id"] or row["person_id"] == scope["person_id"]
                ) and all(t.casefold() in row["quote"].casefold() for t in terms):
                    rows.append(row)
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

    def related(self, targets, scope):
        refs = []
        for target in targets:
            if target["type"] == "evidence" and target.get("sender_account_id"):
                target = {**target, "platform": target["source"], "type": "account"}
            if target["type"] != "account":
                continue
            if scope["person_id"] and target.get("person_id") != scope["person_id"]:
                raise ValueError("person constraint violation")
            rows, more = self.service.sources.related_conversations(
                target, scope, self.cancel, min(self.deadline, time.monotonic() + 3)
            )
            for item in rows:
                self.add([item["evidence"]])
                c = item["contact"]
                c["relation_evidence"] = [item["evidence"]["id"]]
                c["relation_basis"] = item.get(
                    "relation_basis", "same_scoped_account_speech"
                )
                refs.append(self.ref("contact", c, c["key"]))
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

    def context(self, targets, scope):
        found = []
        for target in targets:
            if target["type"] != "evidence" or target["source"] == "recording":
                continue
            selected = {**scope, "relationship": "topic"}
            rows = (
                self.service.sources.context(target, selected)
                if target["origin"] == "cache"
                else []
            )
            found.extend(self.add(rows))
            if self.result["_request"].get("expand", True):
                try:
                    self.verify_remote()
                    if self.result["metrics"]["source_records_read"] + 7 > 100:
                        raise ChatDataError("SOURCE_REQUEST_BUDGET_REACHED", 429)
                    part = self.remote(
                        "/v1/context",
                        {"record_id": target["record_id"], "before": 2, "after": 4},
                    )
                    records = part.get("items")
                    if not isinstance(records, list) or len(records) > 7:
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
                    found.extend(
                        self.add(
                            [
                                self.service.sources.evidence(r, "mac", "context")
                                for r in records
                                if scope["sent_from"]
                                <= (r.get("sent_at") or -1)
                                <= scope["sent_to"]
                            ]
                        )
                    )
                except ChatDataError as exc:
                    self.note("上下文未完整核对：" + exc.code)
        return list(dict.fromkeys(found))
