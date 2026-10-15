"""Chat Query V1: bounded retrieval, durable receipt and evidence-only answers."""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from threading import Event, Lock, RLock, Thread

from allday_asr.v3.domain.chat_data import scope_key
from allday_asr.v3.ports.chat_data import (
    CONTRACT,
    ChatCacheStore,
    ChatDataError,
    ChatDataSource,
)

SCOPE_FIELDS = {
    "platform",
    "source_account_id",
    "conversation_id",
    "sender_account_id",
    "sent_from",
    "sent_to",
    "keyword",
}
RECOVER_ERRORS = {
    "DATASET_MISMATCH",
    "SOURCE_CHANGED",
    "CURSOR_EXPIRED",
    "SCOPE_MISMATCH",
    "SNAPSHOT_MISMATCH",
    "CURSOR_KIND_MISMATCH",
}
OFFLINE_ERRORS = {"MAC_OFFLINE", "CHAT_NOT_CONFIGURED"}


def checked_scope(value, *, default_recent=False):
    if not isinstance(value, dict) or set(value) - SCOPE_FIELDS:
        raise ChatDataError("INVALID_SCOPE", 400)
    scope = {k: v for k, v in value.items() if v is not None and v != ""}
    for key, value in scope.items():
        if key in {"sent_from", "sent_to"}:
            if type(value) is not int or value < 0:
                raise ChatDataError("INVALID_TIME", 400)
        elif not isinstance(value, str) or not value or len(value) > 200:
            raise ChatDataError("INVALID_SCOPE", 400)
    if "platform" in scope and scope["platform"] not in {"qq", "wechat"}:
        raise ChatDataError("INVALID_SCOPE", 400)
    if scope.get("sent_from", 0) > scope.get("sent_to", 2**63 - 1):
        raise ChatDataError("INVALID_TIME", 400)
    if default_recent and not {"sent_from", "sent_to"} & scope.keys():
        scope["sent_from"] = int(time.time()) - 30 * 86400
    return scope


def bounded_int(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ChatDataError("INVALID_LIMIT", 400)
    return value


class ChatQueries:
    def __init__(
        self,
        root: Path,
        source: ChatDataSource,
        cache: ChatCacheStore,
        answer_generator,
        model=None,
        *,
        start_worker=True,
    ):
        self.root = root
        self.cache = cache
        self.source = source
        self._answer_generator = answer_generator
        self.model = model
        self._stop = Event()
        self._sync_lock = RLock()
        self._answer_lock = Lock()
        self._jobs = {}
        self._threads = []
        self._last_discovery = 0
        self._paused = (
            True  # Explicit resume starts finite polls; startup never expands scope.
        )
        self._worker = Thread(target=self._loop, daemon=True, name="chat-index-sync")
        if start_worker:
            self._worker.start()

    def close(self):
        self._stop.set()
        for event in list(self._jobs.values()):
            event.set()
        if self._worker.is_alive():
            self._worker.join(timeout=50)
        for thread in self._threads:
            thread.join(timeout=50)

    def discover(self, *, force=False):
        if not force and time.monotonic() - self._last_discovery < 10:
            return self.cache.get("status")
        status = self.source.request("/v1/status")
        contract = self.source.request("/v1/contract")
        if (
            status.get("api_contract_version") != CONTRACT
            or contract.get("api_contract_version") != CONTRACT
            or status.get("schema_version") != 1
        ):
            raise ChatDataError("CHAT_CONTRACT_UNSUPPORTED", 409)
        self.cache.activate(status["dataset_id"])
        self.cache.put("status", status)
        self.cache.put("contract", contract)
        self.cache.put("coverage", self.source.request("/v1/coverage"))
        self.cache.put("last_contact_at", time.time())
        self._last_discovery = time.monotonic()
        return status

    def configure(self, base_url, token):
        self._paused = True
        self._last_discovery = 0
        result = self.source.configure(base_url, token)
        return result

    def status(self):
        error = None
        try:
            self.discover()
        except ChatDataError as exc:
            error = exc.code
        return {
            "connection": {
                "base_url": getattr(self.source, "base_url", ""),
                "configured": getattr(self.source, "configured", True),
            },
            "contract_version": CONTRACT,
            "source": self.cache.get("status"),
            "coverage": self.cache.get("coverage"),
            "error": error,
            "last_contact_at": self.cache.get("last_contact_at"),
            "cache": self.cache.health(),
            "scopes": self.cache.scopes(),
            "paused": self._paused,
            "workset_limit": 5000,
            "limits": {
                "network_seconds": 15,
                "retries": 2,
                "model_seconds": int(os.getenv("ALLDAY_CHAT_MODEL_TIMEOUT", "180")),
                "model_calls": 100,
                "model_batch_seconds": 1800,
            },
            "source_limitations": "Mac V1 仅新增、固定首次修订；修改/撤回/删除、附件下载不支持；历史完整性未知。",
        }

    def directory(self, kind, scope, cursor=None):
        if kind not in {"accounts", "conversations"}:
            raise ChatDataError("INVALID_REQUEST", 400)
        scope = checked_scope(scope)
        if set(scope) - {"platform", "source_account_id"}:
            raise ChatDataError("INVALID_SCOPE", 400)
        key = "directory:" + kind + ":" + scope_key(scope)
        try:
            self.discover()
            page = self.source.request(
                "/v1/" + kind, scope=scope, limit=100, cursor=cursor
            )
            self._page(page)
            page["_cache_dataset"] = self.cache.get("dataset")
            self.cache.put(key + ":" + str(cursor), page)
            return page | {"origin": "mac"}
        except ChatDataError as exc:
            page = self.cache.get(key + ":" + str(cursor))
            if (
                exc.code not in OFFLINE_ERRORS
                or page is None
                or page.get("_cache_dataset") != self.cache.get("dataset")
            ):
                raise
            return page | {
                "origin": "cache",
                "source_error": exc.code,
                "complete": False,
            }

    @staticmethod
    def _page(page):
        if (
            not isinstance(page.get("items"), list)
            or len(page["items"]) > 100
            or type(page.get("has_more")) is not bool
            or "next_cursor" not in page
            or (page["has_more"] and not page["next_cursor"])
        ):
            raise ChatDataError("INVALID_CHAT_PAGE", 502)

    def search(self, scope, cursor=None, limit=50, origin="mac"):
        scope = checked_scope(scope, default_recent=True)
        bounded_int(limit, 1, 100)
        if origin not in {"mac", "cache"}:
            raise ChatDataError("INVALID_REQUEST", 400)
        if origin == "cache":
            return self.cache.search(scope, limit, cursor)
        try:
            status = self.discover()
            page = self.source.request(
                "/v1/messages", scope=scope, cursor=cursor, limit=limit
            )
            self._page(page)
            self.cache.receive(status["dataset_id"], page["items"])
            return page | {
                "origin": "mac",
                "scope": scope,
                "complete": not page["has_more"],
                "coverage_notice": "当前已捕获范围；历史完整性未知",
                "historical_lookup": scope.get("sent_from", 0)
                < int(time.time()) - 30 * 86400,
            }
        except ChatDataError as exc:
            if exc.code not in OFFLINE_ERRORS:
                raise
            # Never apply a Mac pagination cursor to a local page.
            if cursor:
                raise ChatDataError("MAC_OFFLINE_RESTART_IN_CACHE", 503) from None
            return self.cache.search(scope, limit) | {"source_error": exc.code}

    def context(self, rid, before=10, after=10):
        if not isinstance(rid, str) or not rid or len(rid) > 200:
            raise ChatDataError("INVALID_RECORD_ID", 400)
        bounded_int(before, 0, 25)
        bounded_int(after, 0, 25)
        try:
            status = self.discover()
            result = self.source.request(
                "/v1/context", {"record_id": rid, "before": before, "after": after}
            )
            records = result.get("items")
            if (
                not isinstance(records, list)
                or len(records) > 51
                or not any(r["record_id"] == rid for r in records)
            ):
                raise ChatDataError("INVALID_CHAT_CONTEXT", 502)
            anchor = next(r for r in records if r["record_id"] == rid)
            identity = tuple(
                anchor[k] for k in ("platform", "source_account_id", "conversation_id")
            )
            if any(
                tuple(
                    r[k] for k in ("platform", "source_account_id", "conversation_id")
                )
                != identity
                for r in records
            ):
                raise ChatDataError("CONTEXT_IDENTITY_MISMATCH", 502)
            self.cache.receive(status["dataset_id"], records)
            result["origin"] = "mac"
            self.cache.put("context:" + status["dataset_id"] + ":" + rid, result)
            return result
        except ChatDataError as exc:
            if exc.code not in OFFLINE_ERRORS:
                raise
            cached = self.cache.get(
                "context:" + self.cache.get("dataset", "") + ":" + rid
            )
            return (
                (
                    cached
                    | {"origin": "cache", "complete": False, "source_error": exc.code}
                )
                if cached
                else self.cache.context(rid, before, after) | {"source_error": exc.code}
            )

    def start_sync(self, scopes, *, reinitialize=False):
        if not isinstance(scopes, list) or not 1 <= len(scopes) <= 5:
            raise ChatDataError("SELECT_ONE_TO_FIVE_CONVERSATIONS", 400)
        with self._sync_lock:
            status = self.discover(force=True)
            if status.get("status") != "ready":
                raise ChatDataError("INITIALIZING")
            dataset = status["dataset_id"]
            old = self.cache.scopes()
            prepared = []
            for raw in scopes:
                scope = checked_scope(raw, default_recent=True)
                if (
                    not {"platform", "source_account_id", "conversation_id"}
                    <= scope.keys()
                    or "keyword" in scope
                ):
                    raise ChatDataError("SYNC_REQUIRES_EXPLICIT_CONVERSATION", 400)
                key = scope_key({"dataset": dataset, "scope": scope})
                prepared.append((key, scope))
            active_keys = {
                r["id"] for r in old if r["dataset"] == dataset and r["enabled"]
            }
            if len(active_keys | {key for key, _ in prepared}) > 5:
                raise ChatDataError("ACTIVE_SCOPE_LIMIT_REINITIALIZE_EXPLICITLY", 400)
            for key, scope in prepared:
                existing = self.cache.scope(key)
                if existing and reinitialize:
                    self.cache.reset_scope(key)
                    existing = None  # Cache and user state are deliberately retained.
                if not existing:
                    snap = self.source.request("/v1/snapshots", {"scope": scope})
                    self.cache.create_scope(dataset, scope, snap)
                self.cache.enable_scope(key, True)
            self._paused = False
        return {"scopes": self.cache.scopes(), "paused": False}

    def controls(self, action, scope_id=None):
        if action == "retire-scope" and scope_id:
            self.cache.enable_scope(scope_id, False)
        elif action == "pause":
            self._paused = True
        elif action == "resume":
            self._paused = False
        elif action == "retry-index":
            self.cache.retry_index()
        else:
            raise ChatDataError("INVALID_REQUEST", 400)
        return {"paused": self._paused, "cache": self.cache.health()}

    def sync_once(self, max_pages=10, seconds=60):
        deadline = time.monotonic() + seconds
        with self._sync_lock:
            for row in self.cache.scopes():
                if not row["enabled"]:
                    continue
                if self._stop.is_set() or self._paused or time.monotonic() >= deadline:
                    break
                if row.get("error") in RECOVER_ERRORS:
                    continue  # Never restart history silently.
                try:
                    current = self.discover()
                    if current["dataset_id"] != row["dataset"]:
                        raise ChatDataError("DATASET_MISMATCH", 409)
                    retries = 0
                    for _ in range(max_pages):
                        if (
                            self._stop.is_set()
                            or self._paused
                            or time.monotonic() >= deadline
                        ):
                            break
                        row = self.cache.scope(row["id"])
                        total = sum(
                            r["received"]
                            for r in self.cache.scopes()
                            if r["dataset"] == row["dataset"] and r["enabled"]
                        )
                        snapshot_phase = row["phase"] == "snapshot"
                        remaining = 5000 - total if snapshot_phase else 100
                        if remaining <= 0:
                            raise ChatDataError("WORKSET_BUDGET_REACHED")
                        try:
                            page = self.source.request(
                                "/v1/messages" if snapshot_phase else "/v1/changes",
                                scope=row["scope"],
                                limit=min(100, remaining),
                                cursor=row["page_cursor"]
                                if snapshot_phase
                                else row["receive_cursor"],
                                **(
                                    {"snapshot_id": row["snapshot"]}
                                    if snapshot_phase
                                    else {}
                                ),
                            )
                        except ChatDataError as exc:
                            if (
                                exc.code
                                in {"SERVICE_BUSY", "QUERY_TIMEOUT", "MAC_OFFLINE"}
                                and retries < 2
                            ):
                                retries += 1
                                continue
                            raise
                        self._page(page)
                        if page["has_more"] and page["next_cursor"] == (
                            row["page_cursor"]
                            if snapshot_phase
                            else row["receive_cursor"]
                        ):
                            raise ChatDataError("NON_ADVANCING_CURSOR", 502)
                        progress = {
                            "page_cursor": page["next_cursor"]
                            if snapshot_phase
                            else row["page_cursor"],
                            "receive_cursor": row["boundary"]
                            if snapshot_phase
                            else page["next_cursor"],
                            "phase": "snapshot"
                            if snapshot_phase and page["has_more"]
                            else "changes",
                        }
                        self.cache.receive(
                            row["dataset"],
                            page["items"],
                            scope_id=row["id"],
                            progress=progress,
                            changes=not snapshot_phase,
                        )
                        if not page["has_more"] and not snapshot_phase:
                            break
                except ChatDataError as exc:
                    self.cache.scope_error(row["id"], exc.code)
                    if exc.code == "WORKSET_BUDGET_REACHED":
                        self._paused = True
        return self.cache.scopes()

    def _loop(self):
        next_sync = 0
        while not self._stop.is_set():
            try:
                self.cache.process(100)
                if not self._paused and time.monotonic() >= next_sync:
                    self.sync_once(max_pages=10, seconds=60)
                    next_sync = time.monotonic() + 30
            except Exception:
                self.cache.put("worker_error", "CHAT_WORKER_FAILED")
                self._paused = True
            self._stop.wait(1)

    def submit_answer(self, question, scope, origin="mac"):
        if (
            not isinstance(question, str)
            or not question.strip()
            or len(question) > 2000
        ):
            raise ChatDataError("INVALID_QUESTION", 400)
        scope = checked_scope(scope, default_recent=True)
        if origin not in {"mac", "cache"}:
            raise ChatDataError("INVALID_REQUEST", 400)
        if not self._answer_lock.acquire(blocking=False):
            raise ChatDataError("ANSWER_BUSY")
        jid = uuid.uuid4().hex
        cancel = Event()
        self._jobs[jid] = cancel
        self.cache.job(jid, "pending")
        thread = Thread(
            target=self._answer_job,
            args=(jid, question, scope, cancel, origin),
            daemon=True,
            name="chat-answer",
        )
        self._threads = [t for t in self._threads if t.is_alive()] + [thread]
        thread.start()
        return {"id": jid, "state": "pending"}

    def cancel_answer(self, jid):
        event = self._jobs.get(jid)
        if event:
            event.set()
        return self.cache.job(jid)

    def _answer_job(self, jid, question, scope, cancel, origin):
        try:
            self.cache.job(jid, "running")
            result = self.answer(question, scope, cancel, origin)
            self.cache.job(jid, "done", result)
        except ChatDataError as exc:
            self.cache.job(
                jid,
                "cancelled" if exc.code == "CANCELLED" else "failed",
                error=exc.code,
            )
        except TimeoutError:
            self.cache.job(jid, "failed", error="MODEL_TIMEOUT")
        except Exception:
            self.cache.job(jid, "failed", error="MODEL_FAILED")
        finally:
            self._jobs.pop(jid, None)
            self._answer_lock.release()

    def answer(self, question, scope, cancel, origin="mac"):
        return self._answer_generator(self, question, scope, cancel, origin)
