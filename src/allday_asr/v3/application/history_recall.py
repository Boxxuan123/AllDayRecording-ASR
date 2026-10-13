"""On-demand bounded retrieval over existing facts. No business command dependencies."""

import json
import re
import sqlite3
import time
from threading import Event, RLock
from allday_asr.v3.adapters.sqlite.source_identity_repository import SourceIdentities
from allday_asr.v3.adapters.sqlite.recall_sources import RecallSources, chat_evidence
from allday_asr.v3.adapters.chat_data.mac import MacChatData
from allday_asr.v3.adapters.chat_data.answer import (
    INSTRUCTIONS,
    SCHEMA,
    verified_quote_text,
)
from allday_asr.v3.domain.chat_data import packed
from allday_asr.v3.domain.source_identity import resolve_chat
from allday_asr.v3.domain.memory_content import safe_memory
from allday_asr.v3.ports.chat_data import ChatDataError

LIMITS = {
    "records": 300,
    "page_size": 20,
    "pages": 15,
    "source_requests": 6,
    "model_calls": 1,
    "seconds": 30,
    "retention_seconds": 300,
}
NOTICE = "仅已授权缓存／有界取回；历史完整性未知，未找到不表示未发生。来源修订传播能力以 Mac 为准。"


class HistoryRecall:
    def __init__(self, database, root, source=None, model=None):
        self.root = root
        self.identities = SourceIdentities(database, root / "chat/cache.sqlite3")
        self.sources = RecallSources(self.identities)
        self.source = source or MacChatData(
            config_path=root / "chat/connection.json", timeout=3
        )
        self.model = model
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
            with self.lock:
                self.results.clear()
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
        if result["identity_version"] != self.identities.version():
            with self.lock:
                self.results.pop(key, None)
            raise ValueError("来源或人物关联已变化，旧结果已失效，请重新查询")
        recordings = [e for e in result["evidence"] if e["source"] == "recording"]
        if recordings:
            from allday_asr.v3.adapters.sqlite.recall_sources import CANONICAL

            with self.identities.database.read() as c:
                current = {
                    r[0]: r[1]
                    for r in c.execute(
                        "SELECT u.utterance_id,u.revision FROM utterances u JOIN recording_sessions s ON s.session_id=u.session_id WHERE u.status='active' AND s.tombstoned_at IS NULL AND "
                        + CANONICAL
                        + " AND u.utterance_id IN ("
                        + ",".join("?" for _ in recordings)
                        + ")",
                        [e["utterance_id"] for e in recordings],
                    )
                }
            if any(current.get(e["utterance_id"]) != e["revision"] for e in recordings):
                with self.lock:
                    self.results.pop(key, None)
                raise ValueError("原转写已修订，旧结果已失效，请重新查询")
        return result

    def page(self, owner, qid, page=0):
        if type(page) is not int or not 0 <= page < 15:
            raise ValueError("查询页码无效")
        value = self._saved(owner, qid)
        return {
            **value,
            "evidence": value["evidence"][page * 20 : (page + 1) * 20],
            "page": page,
            "has_more": (page + 1) * 20 < len(value["evidence"]),
        }

    def query(self, owner, payload):
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
            raise ValueError("每次请选择最多 366 天；默认最近 30 天")
        people = self.identities.people()
        if scope["person_id"] and scope["person_id"] not in people:
            raise ValueError("人物尚未确认")
        if any(type(payload.get(k, True)) is not bool for k in ["expand", "summarize"]):
            raise ValueError("查询选项无效")
        terms = self.terms(question, keyword)
        identity_version = self.identities.version()
        started = time.monotonic()
        deadline = started + 30
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
        result = {
            "query_id": qid,
            "scope": scope,
            "terms": terms,
            "identity_version": identity_version,
            "evidence": [],
            "claims": [],
            "unknown": [],
            "status": "partial",
            "coverage": [NOTICE],
            "model_status": "not_requested",
            "metrics": {
                "hits": 0,
                "retrieved": 0,
                "model_calls": 0,
                "source_requests": 0,
                "elapsed_ms": 0,
            },
            "limits": LIMITS,
        }
        try:
            if scope["source"] != "recording":
                rows, more = self.sources.chat(
                    scope, terms, 100, cancel, min(deadline, started + 3)
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
            if (
                len(result["evidence"]) < 5
                and payload.get("expand", True)
                and scope["source"] != "recording"
            ):
                self._expand(result, scope, terms, cancel, deadline)
            result["evidence"] = list({r["id"]: r for r in result["evidence"]}.values())
            result["metrics"]["hits"] = len(result["evidence"])
            self._contexts(result, people, cancel, deadline)
            result["evidence"] = list(
                {r["id"]: r for r in reversed(result["evidence"])}.values()
            )[:300]
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
                result["answer"] = (
                    "当前范围未找到记录，不能据此判断没有发生。可指定更短关键词、人物或日期。"
                )
            self._checkpoint(cancel, deadline)
        except (ChatDataError, sqlite3.OperationalError, TimeoutError):
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
        result["metrics"]["elapsed_ms"] = round((time.monotonic() - started) * 1000)
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
        if keyword.strip():
            return [keyword.strip()]
        # Reuse existing topic labels if they literally occur in this question.
        with self.identities.database.read() as c:
            row = c.execute(
                "SELECT value_json FROM cross_day_index_revisions ORDER BY revision DESC LIMIT 1"
            ).fetchone()
        topics = (
            [
                t["label"]
                for t in json.loads(row[0]).get("topics", [])
                if t.get("label") and t["label"] in question
            ]
            if row
            else []
        )
        if topics:
            return topics[:3]
        cleaned = re.sub(
            r"之前|上次|后来|讨论过|讨论|说到|说过|哪些|什么|当时|提到的|考虑因素|分别在|有没有|有明确|回复|决定|专业|当时|老师|关于|和某人|QQ|微信|录音|里面|里的|里的|了吗|吗|的|我和|我|是|在",
            " ",
            question,
            flags=re.I,
        )
        return [v for v in re.split(r"[\s，。！？、？?；：]+", cleaned) if len(v) >= 2][
            :3
        ] or [question.strip()]

    @staticmethod
    def _checkpoint(cancel, deadline):
        if cancel.is_set():
            raise ChatDataError("CANCELLED", 409)
        if time.monotonic() >= deadline:
            raise ChatDataError("QUERY_TIMEOUT", 408)

    def _expand(self, result, scope, terms, cancel, deadline):
        ids = self.identities
        manual = ids.manual()
        people = ids.people()
        if not ids.chat_path.exists():
            result["coverage"].append("没有已授权聊天缓存范围")
            return
        scopes = []
        if scope["person_id"]:
            for raw, person in manual.items():
                if person == scope["person_id"] or (
                    person == "self" and people[scope["person_id"]]["kind"] == "self"
                ):
                    platform, account, sender = json.loads(raw)
                    if scope["source"] in {"all", platform}:
                        scopes.append(
                            {
                                "platform": platform,
                                "source_account_id": account,
                                "sender_account_id": sender,
                            }
                        )
            if people[scope["person_id"]]["kind"] == "self":
                with ids.cache.connect() as c:
                    rows = c.execute(
                        "SELECT platform,source,account FROM chat_identity_observations WHERE dataset=? AND is_self=1 AND account IS NOT NULL GROUP BY platform,source,account LIMIT 3",
                        (ids.cache.get("dataset", ""),),
                    ).fetchall()
                for platform, account, sender in rows:
                    if packed([platform, account, sender]) not in manual and scope[
                        "source"
                    ] in {"all", platform}:
                        scopes.append(
                            {
                                "platform": platform,
                                "source_account_id": account,
                                "sender_account_id": sender,
                            }
                        )
        else:
            scopes = [
                r["scope"]
                for r in ids.cache.scopes()
                if r["enabled"]
                and scope["source"] in {"all", r["scope"].get("platform")}
            ]
        if not scopes:
            result["coverage"].append("未确认可用的来源身份或未登记范围；未扩大检索")
            return
        if len(scopes) > 2:
            result["coverage"].append("跨来源取回仅覆盖前两个已授权身份／会话")
        try:
            self._checkpoint(cancel, deadline)
            result["metrics"]["source_requests"] += 1
            status = self.source.request("/v1/status")
            if status.get("dataset_id") != ids.cache.get("dataset"):
                raise ChatDataError("DATASET_MISMATCH", 409)
            for selected in scopes[:2]:
                for term in terms:
                    self._checkpoint(cancel, deadline)
                    if (
                        result["metrics"]["source_requests"] >= 6
                        or len(result["evidence"]) >= 260
                    ):
                        return
                    remote = {
                        **selected,
                        "sent_from": scope["sent_from"],
                        "sent_to": scope["sent_to"],
                        "keyword": term,
                    }
                    result["metrics"]["source_requests"] += 1
                    page = self.source.request("/v1/messages", scope=remote, limit=40)
                    records = page.get("items")
                    if not isinstance(records, list) or len(records) > 40:
                        raise ChatDataError("INVALID_CHAT_PAGE", 502)
                    for rec in records:
                        ids.cache._record(rec)
                        if (
                            any(
                                rec.get(k) != v
                                for k, v in selected.items()
                                if k
                                in {
                                    "platform",
                                    "source_account_id",
                                    "conversation_id",
                                    "sender_account_id",
                                }
                            )
                            or not scope["sent_from"]
                            <= rec.get("sent_at", -1)
                            <= scope["sent_to"]
                        ):
                            raise ChatDataError("CONTEXT_IDENTITY_MISMATCH", 502)
                        person, _ = resolve_chat(rec, manual, people)
                        if scope["person_id"] and person != scope["person_id"]:
                            continue
                        if term not in (rec.get("text") or ""):
                            continue
                        result["evidence"].append(
                            chat_evidence(rec, ids, people, manual, "mac")
                        )
                    if page.get("has_more"):
                        result["coverage"].append(
                            "Mac 仍有后续页；本轮仅取回首屏 40 条"
                        )
        except ChatDataError as exc:
            result["coverage"].append("来源不可用：" + exc.code + "；已保留本地证据")
            result["source_status"] = "unavailable"

    def _contexts(self, result, people, cancel, deadline):
        ids = self.identities
        manual = ids.manual()
        for row in list(result["evidence"])[:3]:
            if row["source"] == "recording" or row["origin"] != "cache":
                continue
            self._checkpoint(cancel, deadline)
            part = ids.cache.context(row["record_id"], 2, 4)
            existing = {r["id"] for r in result["evidence"]}
            for rec in part["items"]:
                if "chat:" + rec["record_id"] not in existing:
                    result["evidence"].append(
                        chat_evidence(rec, ids, people, manual, "cache", "context")
                    )
        result["coverage"].append(
            "邻近上下文最多 3 个命中各前 2／后 4 条；其他发言者保留独立身份。关键词未命中的远期答复可能未覆盖。"
        )

    def _summarize(self, result, question, cancel, deadline):
        # Exact quotes remain available on every model failure. One invocation, no retries.
        rows = result["evidence"][:20]
        if len(result["evidence"]) > 20:
            result["coverage"].append("归纳只使用前 20 条依据")
        payload = {
            "question": question,
            "scope": result["scope"],
            "records": rows,
            "evidence_complete": False,
            "known_gaps": result["coverage"],
        }
        result["metrics"]["model_calls"] = 1
        try:
            if self.model:
                value = self.model(payload, cancel)
            else:
                from allday_asr.v3.adapters.codex.model_execution_runner import (
                    ModelExecutionRunner,
                )

                seconds = min(20, int(deadline - time.monotonic()))
                if seconds < 2:
                    raise TimeoutError()
                runner = ModelExecutionRunner(
                    self.root / "chat/model-receipts",
                    timeout_seconds=seconds,
                    max_attempts=1,
                    max_batch_calls=1,
                    batch_deadline_seconds=seconds,
                    max_prompt_bytes=60000,
                )
                workdir = self.root / "chat/model-workspace"
                workdir.mkdir(parents=True, exist_ok=True)
                answer = runner.run(
                    task="history-recall-v1",
                    batch_id="history-recall:" + result["query_id"],
                    prompt=packed(payload),
                    instructions=INSTRUCTIONS
                    + " Use the evidence id field for citations. Explicitly distinguish discussion, plans, decisions and self-reported execution. Do not attribute another person's suggestions or heard speech to the user's decisions. Present contradictions side by side. Keep original quotations separate from summaries; label every inference. All coverage is partial.",
                    schema=SCHEMA,
                    model=None,
                    effort="low",
                    workdir=workdir,
                    cancel_event=cancel,
                )
                value = json.loads(answer.final_response)
            evidence = {r["id"]: r for r in rows}
            if (
                not isinstance(value, dict)
                or not isinstance(value.get("claims"), list)
                or len(value["claims"]) > 20
                or not isinstance(value.get("unknown"), list)
            ):
                raise ValueError("invalid answer")
            for claim in value["claims"]:
                if (
                    set(claim) != {"kind", "text", "citations"}
                    or claim["kind"] not in {"quote", "summary", "inference"}
                    or not isinstance(claim["text"], str)
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
            result["model_status"] = "available"
        except Exception:
            result["model_status"] = "unavailable"
            result["coverage"].append(
                "归纳模型不可用或证据校验失败；请直接查看命中的原文"
            )

    def context(self, owner, qid, evidence_id):
        value = self._saved(owner, qid)
        row = next((e for e in value["evidence"] if e["id"] == evidence_id), None)
        if not row:
            raise ValueError("原文依据不属于本轮查询")
        if row["source"] == "recording":
            with self.identities.database.read() as c:
                raw = c.execute(
                    "SELECT revision,status FROM utterances WHERE utterance_id=?",
                    (row["utterance_id"],),
                ).fetchone()
            if not raw or raw[0] != row["revision"] or raw[1] != "active":
                raise ValueError("原转写已变化，请重新查询")
            return {
                "status": "recording",
                "evidence": [row],
                "notice": "使用现有播放引用片段或原转写入口",
            }
        ids = self.identities
        people = ids.people()
        manual = ids.manual()
        try:
            with self.lock:
                if value["metrics"]["source_requests"] >= 6:
                    raise ChatDataError("SOURCE_REQUEST_BUDGET_REACHED", 429)
                value["metrics"]["source_requests"] += 1
            part = self.source.request(
                "/v1/context", {"record_id": row["record_id"], "before": 2, "after": 4}
            )
            records = part.get("items", [])
            if not isinstance(records, list) or len(records) > 7:
                raise ChatDataError("INVALID_CHAT_CONTEXT", 502)
            for r in records:
                ids.cache._record(r)
                if (r["platform"], r["source_account_id"], r["conversation_id"]) != (
                    row["source"],
                    row["source_account_id"],
                    row["conversation_id"],
                ):
                    raise ChatDataError("CONTEXT_IDENTITY_MISMATCH", 502)
            anchor = next(
                (r for r in records if r["record_id"] == row["record_id"]), None
            )
            if not anchor or anchor["record_revision"] != row["revision"]:
                raise ChatDataError("SOURCE_CHANGED", 409)
            return safe_memory(
                {
                    "status": "available",
                    "evidence": [
                        chat_evidence(r, ids, people, manual, "mac", "context")
                        for r in records
                    ],
                    "notice": "Mac 当前可用上下文；引用对象不是发送者",
                }
            )
        except ChatDataError as exc:
            return {
                "status": "unavailable",
                "evidence": [row],
                "notice": "来源未成功打开（"
                + exc.code
                + "）；这里只显示本轮已取回片段",
            }
