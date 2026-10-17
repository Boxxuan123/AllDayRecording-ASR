"""On-demand bounded retrieval over existing facts. No business command dependencies."""

import re
import time
from copy import deepcopy
from threading import Event, RLock
from allday_asr.v3.domain.memory_content import safe_memory
from allday_asr.v3.domain.recall_language import plan
from .recall_understanding import interpret, apply_contact, choose
from .recall_actions import lookup_contacts, retry_summary
from allday_asr.v3.domain.recall_conversation import native_present
from allday_asr.v3.ports.chat_data import ChatDataError, ChatDataSource
from allday_asr.v3.ports.history_recall import (
    RecallRepository,
    RecallIdentities,
    RecallModel,
)
from .recall_retrieval import expand, contexts

LIMITS = {
    "records": 300,
    "page_size": 20,
    "pages": 15,
    "source_requests": 18,
    "retrieval_requests": 12,
    "model_calls": 4,
    "directory_requests": 6,
    "contact_lookup_refreshes": 2,
    "explicit_summary_retries": 1,
    "seconds": 90,
    "retention_seconds": 300,
}
NOTICE = "仅已授权缓存／有界取回；历史完整性未知，未找到不表示未发生。来源修订传播能力以 Mac 为准。"


class HistoryRecall:
    def __init__(
        self,
        identities: RecallIdentities,
        sources: RecallRepository,
        source: ChatDataSource,
        model: RecallModel,
        *,
        root=None,
        directory=None,
        planner=None,
        investigator=None,
        preferences=None,
    ):
        self.preferences = preferences
        self.root = root
        self.investigator = investigator
        self.directory, self.planner = directory, planner
        self.identities, self.sources, self.source, self.model = (
            identities,
            sources,
            source,
            model,
        )
        self.lock = RLock()
        self.active = {}
        self.results = {}
        self.cancelled = {}

    def execute(self, owner, payload):
        if not isinstance(payload, dict):
            raise ValueError("请求无效")
        action = payload.get("action")
        if action == "catalog":
            return self.identities.catalog(payload.get("offset", 0))
        if action == "identity":
            result = self.identities.decide(payload.get("decision", {}), owner)
            return result
        if action == "cancel":
            key = (owner, self._id(payload.get("query_id")))
            with self.lock:
                self.cancelled[key] = time.monotonic()
                if key in self.active:
                    self.active[key].set()
                self.results.pop(key, None)
            return {"status": "cancelled"}
        if action == "page":
            return self.page(owner, payload.get("query_id"), payload.get("page", 0))
        if action == "context":
            return self.context(
                owner, payload.get("query_id"), payload.get("evidence_id")
            )
        if action in {"remember_target", "forget_target"}:
            from .recall_actions import target_preference

            return target_preference(self, owner, payload)
        if action == "choose":
            return choose(self, owner, payload)
        if action == "contacts":
            return lookup_contacts(self, owner, payload)
        if action == "retry_summary":
            return retry_summary(self, owner, payload)
        if action == "query":
            return self.query(owner, payload)
        raise ValueError("未知回忆操作")

    @staticmethod
    def _id(value):
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{8,80}", value
        ):
            raise ValueError("查询编号无效")
        return value

    def _prune(self):
        now = time.monotonic()
        self.results = {k: v for k, v in self.results.items() if now - v[0] < 300}
        self.cancelled = {k: v for k, v in self.cancelled.items() if now - v < 300}
        while len(self.results) > 8:
            self.results.pop(next(iter(self.results)))
        while len(self.cancelled) > 64:
            self.cancelled.pop(next(iter(self.cancelled)))
    def _saved(self, owner, qid):
        key = (owner, self._id(qid))
        with self.lock:
            self._prune()
            value = self.results.get(key)
        if not value:
            raise ValueError("查询已过期或取消，请重新查询")
        result = value[1]
        if result["scope"]["source"]!="recording" and self.directory and getattr(self.directory,"access_denied",False):
            raise ChatDataError("SOURCE_AUTHORIZATION_REVOKED",403)
        if result["identity_version"] != self.identities.version():
            if self.sources.identity_changed(result["evidence"]):
                with self.lock:
                    self.results.pop(key, None)
                raise ValueError("来源或人物关联已变化，旧结果已失效，请重新查询")
            result["identity_version"] = self.identities.version()
        self._invalidate(result, self.sources.changed(result["evidence"]))
        return result
    def page(self, owner, qid, page=0):
        if type(page) is not int or not 0 <= page < 15:
            raise ValueError("查询页码无效")
        value = self._saved(owner, qid)
        return deepcopy(
            {
                **{
                    k: v
                    for k, v in value.items()
                    if k not in {"continuations"} and not k.startswith("_")
                },
                "evidence": value["evidence"][page * 20 : (page + 1) * 20],
                "page": page,
                "has_more": (page + 1) * 20 < len(value["evidence"]),
            }
        )

    def query(self, owner, payload, selected=None, prepared=None):
        from .recall_cache_budget import query
        return query(self, owner, payload, selected, prepared)

    def _query(self, owner, payload, selected=None, prepared=None):
        allowed = {
            "action",
            "query_id",
            "question",
            "keyword",
            "person_id",
            "source",
            "sent_from",
            "sent_to",
            "expand",
            "summarize",
            "timezone",
            "previous_query_id",
        }
        if set(payload) - allowed:
            raise ValueError("查询字段无效")
        qid = self._id(payload.get("query_id"))
        key = (owner, qid)
        question = payload.get("question", "")
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 1000:
            raise ValueError("请输入 1 至 1000 字的问题")
        keyword = payload.get("keyword", "")
        if not isinstance(keyword, str) or len(keyword) > 100:
            raise ValueError("关键词过长")
        now = int(time.time())
        scope = {
            "person_id": payload.get("person_id", ""),
            "source": payload.get("source", "all"),
            "sent_from": payload.get("sent_from", now - 30 * 86400),
            "sent_to": payload.get("sent_to", now),
        }
        if scope["source"] not in {
            "all",
            "qq",
            "wechat",
            "recording",
        } or not isinstance(scope["person_id"], str):
            raise ValueError("查询来源无效")
        if (
            any(type(scope[k]) is not int for k in ("sent_from", "sent_to"))
            or not 0 <= scope["sent_from"] <= scope["sent_to"]
            or scope["sent_to"] - scope["sent_from"] > 366 * 86400
        ):
            raise ValueError("每次请选择最多 366 天")
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(payload.get("timezone", "Asia/Singapore"))
        except (ValueError, TypeError, KeyError):
            raise ValueError("用户时区无效，请使用 IANA 时区") from None
        people = self.identities.people()
        if scope["person_id"] and scope["person_id"] not in people:
            raise ValueError("指定人物尚未确认；未回退到全部人物")
        if any(type(payload.get(k, True)) is not bool for k in ["expand", "summarize"]):
            raise ValueError("查询选项无效")
        started = time.monotonic()
        deadline = started + (
            prepared.get("_remaining_seconds", 90) if prepared else 90
        )
        cancel = Event()
        with self.lock:
            self._prune()
            if key in self.cancelled:
                return {"status": "cancelled", "query_id": qid}
            if key in self.active:
                raise ValueError("查询正在执行")
            if len(self.active) >= 2:
                raise ValueError("查询繁忙，请取消上一轮或稍后重试")
            for other, event in self.active.items():
                if other[0] == owner:
                    event.set()
            self.active[key] = cancel
        try:
            previous = None
            if payload.get("previous_query_id"):
                self._id(payload["previous_query_id"])
                try:
                    previous = self._saved(owner, payload["previous_query_id"])
                except ValueError:
                    # Expired context grants no scope and must not block a new question.
                    pass
            query_plan = (
                deepcopy(prepared)
                if prepared
                else interpret(
                    self,
                    question,
                    keyword,
                    people,
                    payload,
                    now,
                    cancel,
                    deadline,
                    selected,
                    previous,
                    owner=owner,
                )
            )
            if cancel.is_set():
                with self.lock:
                    self.active.pop(key, None)
                return {"status": "cancelled", "query_id": qid}
            if selected:
                apply_contact(query_plan, selected)
            scope = query_plan["scope"]
            from datetime import datetime
            from zoneinfo import ZoneInfo

            for bound in ("sent_from", "sent_to"):
                scope[bound + "_local"] = datetime.fromtimestamp(
                    scope[bound], ZoneInfo(scope["timezone"])
                ).isoformat()
            if (
                not 0 <= scope["sent_from"] <= scope["sent_to"]
                or scope["sent_to"] - scope["sent_from"] > 366 * 86400
            ):
                raise ValueError("每次请选择最多 366 天")
            terms = query_plan["terms"]
            identity_version = self.identities.version()
        except Exception:
            with self.lock:
                self.active.pop(key, None)
            raise
        result = {
            "query_id": qid,
            "scope": scope,
            "terms": terms,
            "intent": query_plan["intent"],
            "person_candidates": [
                p
                for p in query_plan["person_candidates"]
                if not p["person_id"].startswith("native:")
            ],
            "contact_candidates": query_plan.get("contact_candidates", []),
            "contact_lookup_allowed": query_plan.get("contact_lookup_allowed", False)
            and bool(query_plan["issue"]),
            "summary_retry_available": False,
            "_request": deepcopy(payload),
            "_plan": deepcopy(query_plan),
            "query_understanding": {
                "person_mention": query_plan["person_mention"],
                "time_explicit": query_plan["time_explicit"],
            },
            "latest_status_confirmed": False,
            "identity_version": identity_version,
            "evidence": [],
            "claims": [],
            "unknown": [],
            "status": "partial",
            "coverage": [NOTICE, *query_plan.get("directory_notes", [])],
            "model_status": "not_requested",
            "metrics": {
                "hits": 0,
                "retrieved": 0,
                "model_calls": query_plan.get("understanding_calls", 0),
                "understanding_calls": query_plan.get("understanding_calls", 0),
                "directory_requests": query_plan.get("directory_requests", 0),
                "source_requests": query_plan.get("directory_requests", 0),
                "retrieval_requests": 0,
                "source_records_read": 0,
                "model_elapsed_ms": 0,
                "model_input_ids": [],
                "elapsed_ms": 0,
            },
            "limits": LIMITS,
        }
        if query_plan.get("target_preference"):
            result["target_preference"] = deepcopy(query_plan["target_preference"])
        if query_plan.get("_carried_metrics"):
            carried = query_plan["_carried_metrics"]
            for name in (
                "model_calls",
                "source_requests",
                "retrieval_requests",
                "directory_requests",
                "source_records_read", "body_requests", "source_bytes", "cache_records_read", "cache_bytes",
            ):
                result["metrics"][name] = carried.get(name, 0)
            result["_spent_ms"] = carried.get("elapsed_ms", 0)
        if query_plan.get("agentic"):
            from .recall_investigation import investigate

            try:
                investigate(self, result, query_plan, question, cancel, deadline, now)
            finally:
                with self.lock:
                    self.active.pop(key, None)
            return self._finish(owner, qid, key, cancel, started, result)
        if query_plan["issue"]:
            result.update(
                status="needs_clarification",
                answer=query_plan["issue"],
                unknown=[query_plan["issue"]],
            )
            with self.lock:
                self.active.pop(key, None)
                self.results[key] = (time.monotonic(), safe_memory(result))
            return self.page(owner, qid)
        try:
            self._checkpoint(cancel, deadline)
            if scope.get("relationship") == "conversation":
                result["coverage"].append(
                    "私聊使用来源账号＋会话 ID 取回双方；群聊仅纳入双方的明确回复关系。录音说话人确认不能证明本人在与其交谈，未纳入。"
                )
            if scope["source"] != "recording":
                rows, more = self.sources.chat(
                    scope, terms, 100, cancel, min(deadline, time.monotonic() + 3)
                )
                result["evidence"].extend(rows)
                if more:
                    result["coverage"].append("聊天命中超过本次 100 条上限")
            if scope["source"] not in {"qq", "wechat"}:
                rows, more = self.sources.recordings(
                    scope, terms, 100, cancel, min(deadline, time.monotonic() + 3)
                )
                result["evidence"].extend(rows)
                if more:
                    result["coverage"].append("录音命中超过本次 100 条上限")
            self._checkpoint(cancel, deadline)
            if payload.get("expand", True) and scope["source"] != "recording":
                expand(self, result, scope, query_plan, cancel, deadline)
            if not result["evidence"] and scope.get("relationship") == "conversation":
                from .recall_temporal import nearby_dialogue

                nearby_dialogue(self, result, query_plan, cancel, deadline)
            # Prefer the highest observed revision; source order cannot replay an older answer.
            merged = {}
            for row in result["evidence"]:
                old = merged.get(row["id"])
                if old is None or row["revision"] >= old["revision"]:
                    merged[row["id"]] = row
            result["evidence"] = list(merged.values())
            result["metrics"]["hits"] = sum(
                r["role"] == "match" for r in result["evidence"]
            )
            contexts(self, result, cancel, deadline)
            result["evidence"] = list(
                {r["id"]: r for r in reversed(result["evidence"])}.values()
            )[:300]
            invalid = self.sources.changed(result["evidence"])
            result["evidence"] = [
                r for r in result["evidence"] if r["id"] not in invalid
            ]
            if invalid:
                result["coverage"].append(
                    "已排除确认修订／撤回的旧证据；需重新取回当前来源"
                )
            result["evidence"] = [native_present(r, scope) for r in result["evidence"]]
            result["evidence"].sort(key=lambda e: (e.get("sent_at") or 0, e["id"]))
            result["metrics"]["retrieved"] = len(result["evidence"])
            self._checkpoint(cancel, deadline)
            if result["evidence"]:
                result["answer"] = (
                    f"找到 {result['metrics']['hits']} 条相关发言，附 {len(result['evidence'])} 条原文依据。请按时间核对；发言中的计划和自述不自动代表已决定或已执行。"
                )
                if payload.get("summarize", True):
                    self._summarize(result, question, cancel, deadline)
            else:
                result["status"] = "not_found"
                if scope.get("relationship") == "conversation" and payload.get(
                    "expand", True
                ):
                    result["status"] = "coverage_insufficient"
                result["answer"] = (
                    "当前人物、时间和会话范围未找到可证明双方交流的记录；来源采集或覆盖不足，不能判断没有聊天。录音中某人讲话不等于本人在与其交谈。"
                    if scope.get("relationship") == "conversation"
                    else "当前范围未找到记录，不能据此判断没有发生。可指定更短关键词、人物或日期。"
                )
            self._checkpoint(cancel, deadline)
        except (ChatDataError, TimeoutError):
            if cancel.is_set():
                result["status"] = "cancelled"
                result["evidence"] = []
            else:
                result["status"] = "partial"
                result["coverage"].append("达到时间／来源预算，保留已取回的部分结果")
            result.setdefault("answer", "本轮只取回部分证据，请缩小范围后重试。")
        finally:
            with self.lock:
                self.active.pop(key, None)
        return self._finish(owner, qid, key, cancel, started, result)

    def _finish(self, owner, qid, key, cancel, started, result):
        result["target_remember_available"] = bool(
            self.preferences
            and result["_plan"].get("person_mention")
            and len(result["scope"].get("source_contacts", [])) == 1
            and not result.get("target_preference")
        )
        result["metrics"]["elapsed_ms"] = result.get("_spent_ms", 0) + round(
            (time.monotonic() - started) * 1000
        )
        result["metrics"]["retrieved"] = len(result["evidence"])
        if result["identity_version"] != self.identities.version():
            raise ValueError("查询期间人物关联或来源变化，请重新查询")
        with self.lock:
            if key in self.cancelled or cancel.is_set():
                return {"status": "cancelled", "query_id": qid}
            self.results[key] = (time.monotonic(), safe_memory(result))
            self._prune()
        return self.page(owner, qid)

    def terms(self, question, keyword):
        return plan(question, keyword)["terms"]

    @staticmethod
    def _invalidate(result, ids):
        if not ids:
            return
        result.update(
            status="stale",
            model_status="stale",
            summary_retry_available=False,
            claims=[],
            answer="部分来源已确认修订／撤回，旧结论已失效，请重新查询。",
        )
        if result.get("investigation_version"):
            result["contact_candidates"] = []
            result["contact_lookup_allowed"] = False
        for row in result["evidence"]:
            if row["id"] in ids:
                row["validity"] = "stale"
        result["invalidated_evidence_ids"] = sorted(
            set(result.get("invalidated_evidence_ids", [])) | set(ids)
        )
        notice = "已确认变化与来源离线不同；失效片段仅供审计，不再作为结论依据。"
        if notice not in result["coverage"]:
            result["coverage"].append(notice)

    @staticmethod
    def _checkpoint(cancel, deadline):
        if cancel.is_set():
            raise ChatDataError("CANCELLED", 409)
        if time.monotonic() >= deadline:
            raise ChatDataError("QUERY_TIMEOUT", 408)

    def _summarize(self, result, question, cancel, deadline):
        from .recall_summary import summarize

        return summarize(self, result, question, cancel, deadline)

    def context(self, owner, qid, evidence_id):
        value = self._saved(owner, qid)
        row = next((e for e in value["evidence"] if e["id"] == evidence_id), None)
        if not row:
            raise ValueError("原文依据不属于本轮查询")
        if row.get("validity") == "stale":
            return {
                "status": "stale",
                "query_id": qid,
                "invalidated_evidence_ids": [row["id"]],
                "evidence": [row],
                "notice": "该来源已确认变化，旧结论已失效，请重新查询",
            }
        if row["source"] == "recording":
            return {
                "status": "recording",
                "evidence": [row],
                "notice": "使用现有播放引用片段或原转写入口",
            }
        try:
            from .recall_original_context import read_context
            part = read_context(self, value, row)
            records = part["items"]
            for r in records:
                self.sources.validate_record(r)
                if (r["platform"], r["source_account_id"], r["conversation_id"]) != (
                    row["source"],
                    row["source_account_id"],
                    row["conversation_id"],
                ):
                    raise ChatDataError("CONTEXT_IDENTITY_MISMATCH", 502)
            anchor = next(
                (r for r in records if r["record_id"] == row["record_id"]), None
            )
            if not anchor or anchor["record_revision"] > row["revision"]:
                raise ChatDataError("SOURCE_CHANGED", 409)
            if anchor["record_revision"] < row["revision"]:
                raise ChatDataError("SOURCE_REVISION_BEHIND", 503)
            context_scope = value.get("_evidence_scopes", {}).get(
                row["id"], value["scope"]
            )
            records = self.sources.filter_context(records, context_scope)
            evidence = [native_present(self.sources.evidence(r, "mac", "context"), context_scope) for r in records]
            with self.lock:
                value.setdefault("_context_evidence", {}).update({r["id"]: r for r in evidence})
            return safe_memory(
                {
                    "status": "available",
                    "evidence": evidence,
                    "notice": "Mac 当前可用上下文；引用对象不是发送者",
                }
            )
        except ChatDataError as exc:
            if exc.code in {
                "SOURCE_CHANGED",
                "RECORD_NOT_FOUND",
                "RECORD_DELETED",
                "RECORD_RETRACTED",
            }:
                self.sources.mark_changed(row, exc.code)
                with self.lock:
                    for _, saved in self.results.values():
                        affected = {
                            r["id"]
                            for r in saved["evidence"]
                            if r["id"] == row["id"] and r["revision"] <= row["revision"]
                        }
                        self._invalidate(saved, affected)
                return {
                    "status": "stale",
                    "query_id": qid,
                    "invalidated_evidence_ids": [row["id"]],
                    "evidence": [row],
                    "notice": "来源已确认修订／撤回，旧证据与归纳已失效，请重新查询",
                }
            from .recall_original_context import saved_context
            return {
                "status": "unavailable",
                "query_id": qid,
                "evidence": saved_context(self, value, row),
                "notice": "来源未成功打开（"
                + exc.code
                + "）；缓存仅供核对，当前状态未知",
            }
