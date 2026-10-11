"""Explicit finite jobs: no automatic permanent background model analysis."""

from threading import Event, Lock, Thread
import json
import time
import uuid
from zoneinfo import ZoneInfo
from allday_asr.v3.application.file_lock import day_worker_lock
from .chat_followup_handoff import import_handoff

from allday_asr.v3.application.chat_followups import ChatFollowups
from allday_asr.v3.adapters.sqlite.unit_of_work import SqliteUnitOfWork
from allday_asr.v3.adapters.chat_data.followup_model import (
    FollowupModel,
    VISIBLE_INPUT_BYTE_LIMIT,
    visible_input_bytes,
)
from allday_asr.v3.domain.chat_data import packed
from allday_asr.v3.domain.chat_projection import semantic_value
from allday_asr.v3.domain.chat_followups import conversation_key, VERSION
from allday_asr.v3.ports.chat_data import ChatDataError


class FollowupJobs:
    def __init__(self, core, chat, *, model=None):
        self.root = core.paths.state_dir / "chat" / "followups"
        self.root.mkdir(parents=True, exist_ok=True)
        self.chat = chat
        self.service = ChatFollowups(
            lambda: SqliteUnitOfWork(core.database),
            reminders=core.reminders,
            identity_cache=chat.cache,
        )
        self.model = model or FollowupModel(self.root)
        self._cancel = Event()
        self._lock = Lock()
        self._thread = None
        self._lease = None
        with day_worker_lock(self.root / "worker.lock") as acquired:
            if acquired:
                for job in self.service.jobs():
                    if job["state"] in {"running", "collecting", "pending"}:
                        self.service.save_job(
                            job["id"],
                            "interrupted",
                            {k: v for k, v in job.items() if k not in {"id", "state"}},
                        )

        self._handoff = import_handoff(self)
        from allday_asr.v3.application.chat_followup_identity import rebuild_identity

        self._identity_rebuild = rebuild_identity(self.service, self.chat.cache)

    def scopes(self):
        path = self.root / "config.json"
        if path.exists():
            value = json.loads(path.read_text(encoding="utf8"))
            return value.get("scopes", []), value.get("timezone")
        return [s["scope"] for s in self.chat.cache.scopes() if s["enabled"]][:3], None

    def configure(self, scopes, timezone_name):
        from allday_asr.v3.application.chat_queries import checked_scope

        if not isinstance(scopes, list) or not 1 <= len(scopes) <= 3:
            raise ChatDataError("FOLLOWUP_SCOPE_REQUIRED", 400)
        result = []
        for scope in scopes:
            scope = checked_scope(scope)
            required = {"platform", "source_account_id", "conversation_id"}
            if not required <= scope.keys():
                raise ChatDataError("FOLLOWUP_SCOPE_REQUIRED", 400)
            result.append({k: scope[k] for k in required})
        if timezone_name:
            try:
                ZoneInfo(timezone_name)
            except Exception:
                raise ChatDataError("INVALID_FOLLOWUP_TIMEZONE", 400) from None
        path = self.root / "config.json"
        temporary = path.with_suffix(".part")
        temporary.write_text(
            packed({"scopes": result, "timezone": timezone_name}), encoding="utf8"
        )
        temporary.replace(path)
        if hasattr(self, "_handoff"):
            self._handoff = import_handoff(self)
        return {
            "configured": True,
            "scope_count": len(result),
            "timezone": timezone_name,
        }

    def link_account(self, platform, source, account, person):
        if platform not in {"qq", "wechat"} or any(
            not isinstance(v, str) or not v or len(v) > 200
            for v in (source, account, person)
        ):
            raise ChatDataError("INVALID_FOLLOWUP_ACTOR", 400)
        self.chat.cache.save_manual_identity(platform, source, account, person)
        self._handoff = import_handoff(self)
        return {"saved": True}

    def links(self):
        return self.chat.cache.identity_links()["links"]

    @staticmethod
    def public(job):
        return {
            k: v
            for k, v in job.items()
            if k not in {"records", "batches", "self_account_links"}
        }

    def status(self):
        scopes, tz = self.scopes()
        return {
            "scope_count": len(scopes),
            "selected_scopes": scopes,
            "local_handoff": self._handoff,
            "timezone": tz,
            "jobs": [self.public(j) for j in self.service.jobs()],
            "self_mapping_count": len(
                {x["key"] for x in self.chat.cache.identity_links()["automatic"]}
                | {
                    k
                    for k, v in self.chat.cache.identity_links()["manual"].items()
                    if v == "self"
                }
            ),
            "self_identity": self.chat.cache.identity_links(),
            "identity_rebuild": self._identity_rebuild,
            "identity_refresh": self.chat.cache.identity_refresh_status(),
            "limits": {
                "calls": 12,
                "seconds": 900,
                "unique_messages": None,
                "input_visible_input_bytes": VISIBLE_INPUT_BYTE_LIMIT,
            },
            "calendar": "关联已有录音任务后沿用原提醒；纯聊天事项不自动创建手机提醒",
        }

    def start(
        self,
        *,
        resume_id=None,
        records=None,
        dataset=None,
        coverage=None,
        renew_model_window=False,
        additional_calls=12,
    ):
        if (
            type(renew_model_window) is not bool
            or type(additional_calls) is not int
            or not 1 <= additional_calls <= 12
            or (renew_model_window and not resume_id)
        ):
            raise ChatDataError("INVALID_FOLLOWUP_BUDGET", 400)
        if not self._lock.acquire(blocking=False):
            raise ChatDataError("FOLLOWUP_JOB_RUNNING", 409)
        self._lease = day_worker_lock(self.root / "worker.lock")
        if not self._lease.__enter__():
            self._lease.__exit__(None, None, None)
            self._lease = None
            self._lock.release()
            raise ChatDataError("FOLLOWUP_JOB_RUNNING", 409)
        self._cancel.clear()
        try:
            if resume_id:
                job = self.service.job(resume_id)
                if job["state"] not in {"interrupted", "failed", "paused", "partial"}:
                    raise ChatDataError("FOLLOWUP_JOB_NOT_RESUMABLE", 409)
                # Allow collecting remaining pages and replaying saved responses
                # even when the model budget is exhausted. _run still enforces it
                # immediately before any new model request.
                job_id = resume_id
                value = {k: v for k, v in job.items() if k not in {"id", "state"}}
                if renew_model_window:
                    # Explicit user action authorizes a fresh finite time window;
                    # cumulative calls and the original total 12-call cap stay intact.
                    if value["calls"] >= 12:
                        raise ChatDataError("FOLLOWUP_BUDGET_EXHAUSTED", 429)
                    value.setdefault("model_window_history", []).append(
                        {
                            "deadline_at": value["deadline_at"],
                            "call_limit": value.get("model_call_limit", 12),
                            "calls_used": value["calls"],
                        }
                    )
                    value["deadline_at"] = time.time() + 900
                    value["model_call_limit"] = min(
                        12, value["calls"] + additional_calls
                    )
                    value["model_batch_id"] = (
                        job_id + ":window:" + str(len(value["model_window_history"]))
                    )
                    value["error"] = None
                if value.get("failed_batches"):
                    value.setdefault("failure_history", []).append(
                        {
                            "batches": value["failed_batches"],
                            "count": value["failed_candidates"],
                        }
                    )
                    value["position"] = min(value["failed_batches"])
                    value["failed_batches"] = []
                    value["failed_candidates"] = 0
            else:
                job_id = uuid.uuid4().hex
                value = {
                    "calls": 0,
                    "position": 0,
                    "applied": 0,
                    "duplicate": 0,
                    "failed_candidates": 0,
                    "failed_batches": [],
                    "processing_watermark": 0,
                    "created_at": time.time(),
                    "deadline_at": None,
                    "coverage": coverage or [],
                    "error": None,
                    "records": records,
                    "dataset": dataset,
                    "batches": None,
                    "unknown": [],
                    "self_account_links": self.links(),
                    "timezone": self.scopes()[1],
                }
            self.service.save_job(job_id, "pending", value)
            self._thread = Thread(
                target=self._run,
                args=(job_id, value),
                daemon=True,
                name="chat-followup-finite",
            )
            self._thread.start()
            return self.public(self.service.job(job_id))
        except BaseException:
            if self._lease:
                self._lease.__exit__(None, None, None)
                self._lease = None
            self._lock.release()
            raise

    def _collect(self, value, job_id=None):
        from .chat_followup_collection import collect

        collect(self, value, job_id)

    def _payload(self, records, value):
        conv = conversation_key(records[0]) if records else None
        existing = [
            {
                "event_id": x["event_id"],
                "title": x["payload"].get("title"),
                "status": x["status"],
                "latest_sent_at": x["latest_sent_at"],
                "human_override": bool(x["human_override"]),
                "anchor_id": x["chat"].get("anchor_id"),
                "issue_quote": x["chat"].get("issue_quote"),
                "time": x["chat"].get("time"),
            }
            for x in self.service.list(100)["items"]
            if x["conversation_key"] == conv and x["dataset"] == value["dataset"]
        ]
        return semantic_value(
            {
                "version": VERSION,
                "timezone": value["timezone"],
                "now_utc": value.get("as_of"),
                "self_account_links": value["self_account_links"],
                "records": [
                    {
                        k: r.get(k)
                        for k in (
                            "record_id",
                            "record_revision",
                            "is_from_self",
                            "self_identity_basis",
                            "sender_account_id",
                            "sent_at",
                            "text",
                            "reply",
                            "quote",
                            "forward",
                        )
                    }
                    for r in records
                ],
                "source_namespace": conv,
                "existing_items": existing[:15],
                "existing_items_truncated": max(0, len(existing) - 15),
                "coverage_complete": False,
                "source_limitations": "首次修订；修订/删除未传播，历史完整性未知",
            }
        )

    def _batch(self, value):
        by_conv = {}
        for r in sorted(
            value["records"], key=lambda r: (r.get("sent_at") or 0, r["record_id"])
        ):
            by_conv.setdefault(conversation_key(r), []).append(r)
        batches = []
        for records in by_conv.values():
            batch = []
            for record in records:
                if (
                    visible_input_bytes(self._payload(batch + [record], value))
                    > VISIBLE_INPUT_BYTE_LIMIT - 20000
                ):
                    if not batch:
                        raise ChatDataError("FOLLOWUP_VISIBLE_INPUT_BYTES", 429)
                    batches.append(batch)
                    batch = [record]
                    if (
                        visible_input_bytes(self._payload(batch, value))
                        > VISIBLE_INPUT_BYTE_LIMIT
                    ):
                        raise ChatDataError("FOLLOWUP_VISIBLE_INPUT_BYTES", 429)
                else:
                    batch.append(record)
            if batch:
                batches.append(batch)
        return batches

    def _run(self, job_id, value):
        try:
            if value["records"] is None or any(
                ("scope" in c or "unvisited_scopes" in c) and not c.get("complete")
                for c in value["coverage"]
            ):
                self.service.save_job(job_id, "collecting", value)
                self._collect(value, job_id)
            value["error"] = None
            value.setdefault("failed_batches", [])
            value.setdefault("processing_watermark", value["position"])
            value["as_of"] = value.get("as_of", time.time())
            value["unique_messages"] = len({r["record_id"] for r in value["records"]})
            if value["batches"] is None:
                value["batches"] = self._batch(value)
            else:
                batched = {
                    (r["record_id"], r["record_revision"])
                    for batch in value["batches"]
                    for r in batch
                }
                remaining = [
                    r
                    for r in value["records"]
                    if (r["record_id"], r["record_revision"]) not in batched
                ]
                if remaining:
                    value["batches"].extend(
                        self._batch({**value, "records": remaining})
                    )
            value["batch_count"] = len(value["batches"])
            if value["deadline_at"] is None:
                value["deadline_at"] = time.time() + 900
            while value["position"] < len(value["batches"]):
                if self._cancel.is_set():
                    raise ChatDataError("CANCELLED", 409)
                batch = value["batches"][value["position"]]
                payload = self._payload(batch, value)
                path = self.root / (
                    job_id + "-" + str(value["position"]) + ".response.json"
                )
                if path.exists():
                    saved = json.loads(path.read_text(encoding="utf8"))
                    response = saved["response"]
                    batch = saved.get("source_records", saved["payload"]["records"])
                else:
                    if (
                        value["calls"] >= min(12, value.get("model_call_limit", 12))
                        or time.time() >= value["deadline_at"]
                    ):
                        raise ChatDataError("FOLLOWUP_BUDGET_EXHAUSTED", 429)
                    if visible_input_bytes(payload) > VISIBLE_INPUT_BYTE_LIMIT:
                        raise ChatDataError("FOLLOWUP_VISIBLE_INPUT_BYTES", 429)
                    value["calls"] += 1
                    self.service.save_job(job_id, "running", value)
                    response = self.model(
                        payload,
                        self._cancel,
                        value.get("model_batch_id", job_id),
                        value["deadline_at"] - time.time(),
                    )
                    path.write_text(
                        packed(
                            {
                                "payload": payload,
                                "source_records": batch,
                                "response": response,
                            }
                        ),
                        encoding="utf8",
                    )
                batch_failed = False
                if self.chat.cache.get("dataset") != value["dataset"]:
                    raise ChatDataError("FOLLOWUP_SOURCE_CHANGED", 409)
                for candidate in response["items"]:
                    if self._cancel.is_set():
                        raise ChatDataError("CANCELLED", 409)
                    try:
                        result = self.service.apply(
                            candidate,
                            batch,
                            value["dataset"],
                            value["self_account_links"],
                            timezone_name=value["timezone"],
                            provenance=response.get("_execution"),
                        )
                        value["duplicate"] += result["effect"] == "duplicate"
                        value["applied"] += result["effect"] == "applied"
                    except ChatDataError as exc:
                        batch_failed = True
                        value["failed_candidates"] += 1
                        value["unknown"].append(exc.code)
                value["unknown"].extend(response.get("unknown", []))
                if batch_failed and value["position"] not in value["failed_batches"]:
                    value["failed_batches"].append(value["position"])
                value["position"] += 1
                value["processing_watermark"] = min(
                    value["failed_batches"], default=value["position"]
                )
                self.service.save_job(job_id, "running", value)
            value["finished_at"] = time.time()
            self.service.save_job(
                job_id,
                "partial"
                if value["failed_candidates"]
                or any(not c.get("complete", False) for c in value["coverage"])
                else "completed",
                value,
            )
        except BaseException as exc:
            value["error"] = (
                exc.code if isinstance(exc, ChatDataError) else type(exc).__name__
            )
            self.service.save_job(
                job_id, "paused" if self._cancel.is_set() else "failed", value
            )
        finally:
            if self._lease:
                self._lease.__exit__(None, None, None)
                self._lease = None
            self._lock.release()

    def stop(self):
        self._cancel.set()
        return {"stopping": True}

    def close(self):
        self._cancel.set()
        if self._thread:
            self._thread.join(timeout=10)
