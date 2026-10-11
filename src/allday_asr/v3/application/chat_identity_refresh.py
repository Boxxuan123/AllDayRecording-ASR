"""Finite resumable metadata-only refresh of captured local IDs; never a model job."""

import json
import time
from uuid import uuid4
from threading import Event
from .file_lock import day_worker_lock
from allday_asr.v3.domain.chat_data import packed
from allday_asr.v3.ports.chat_data import ChatDataError, CONTRACT


class IdentityRefresh:
    def __init__(self, cache, source, *, stop=None):
        self.cache, self.source = cache, source
        self.stop = stop or Event()

    def create(self, *, max_records=5000, max_requests=200, seconds=300):
        if (
            type(max_records) is not int
            or not 1 <= max_records <= 5000
            or type(max_requests) is not int
            or not 1 <= max_requests <= 200
            or type(seconds) is not int
            or not 1 <= seconds <= 300
        ):
            raise ChatDataError("INVALID_IDENTITY_BUDGET", 400)
        dataset = self.cache.get("dataset")
        job = uuid4().hex
        with self.cache.connect() as db:
            rows = db.execute(
                "SELECT id,revision FROM records WHERE dataset=? AND deleted=0 ORDER BY id LIMIT ?",
                (dataset, max_records),
            ).fetchall()
            total = db.execute(
                "SELECT COUNT(*) FROM records WHERE dataset=? AND deleted=0", (dataset,)
            ).fetchone()[0]
            value = {
                "state": "pending",
                "requests": 0,
                "max_requests": max_requests,
                "seconds": seconds,
                "elapsed": 0,
                "total": len(rows),
                "outside_budget": total - len(rows),
                "changed": 0,
                "error": None,
            }
            db.execute(
                "INSERT INTO chat_identity_refresh_jobs VALUES(?,?,?)",
                (job, dataset, packed(value)),
            )
            db.executemany(
                "INSERT INTO chat_identity_refresh_items(job,id,revision,state) VALUES(?,?,?,'pending')",
                [(job, r["id"], r["revision"]) for r in rows],
            )
        return job

    def status(self, job):
        with self.cache.connect() as db:
            row = db.execute(
                "SELECT * FROM chat_identity_refresh_jobs WHERE id=?", (job,)
            ).fetchone()
            if not row:
                raise ChatDataError("IDENTITY_JOB_NOT_FOUND", 404)
            value = json.loads(row["value"])
            counts = {
                r[0]: r[1]
                for r in db.execute(
                    "SELECT state,COUNT(*) FROM chat_identity_refresh_items WHERE job=? GROUP BY state",
                    (job,),
                )
            }
        return {
            "id": job,
            "dataset": row["dataset"],
            **value,
            "success": counts.get("done", 0),
            "failed": counts.get("failed", 0),
            "remaining": counts.get("pending", 0),
            "model_calls": 0,
        }

    def _save(self, job, value):
        with self.cache.connect() as db:
            db.execute(
                "UPDATE chat_identity_refresh_jobs SET value=? WHERE id=?",
                (packed(value), job),
            )

    def run(self, job, *, max_batches=50, progress=None):
        if type(max_batches) is not int or not 1 <= max_batches <= 50:
            raise ChatDataError("INVALID_IDENTITY_BUDGET", 400)
        with day_worker_lock(
            self.cache.path.with_name("identity-refresh.lock")
        ) as acquired:
            if not acquired:
                raise ChatDataError("IDENTITY_REFRESH_RUNNING", 409)
            return self._run(job, max_batches, progress)

    def _run(self, job, max_batches, progress):
        start = time.monotonic()
        info = self.status(job)
        value = {
            k: v
            for k, v in info.items()
            if k
            not in {"id", "dataset", "success", "failed", "remaining", "model_calls"}
        }
        charged = value["elapsed"]
        if value.get("active_since"):
            charged += max(0, time.time() - value["active_since"])
        value["state"] = "running"

        def checkpoint():
            value["elapsed"] = charged + time.monotonic() - start
            value["active_since"] = time.time()
            self._save(job, value)

        def request(route, body=None):
            if self.stop.is_set():
                raise ChatDataError("CANCELLED", 409)
            if (
                value["requests"] >= value["max_requests"]
                or charged + time.monotonic() - start >= value["seconds"]
            ):
                raise ChatDataError("IDENTITY_BUDGET_EXHAUSTED", 429)
            value["requests"] += 1
            checkpoint()
            return self.source.request(route, body)

        try:
            contract = request("/v1/contract")
            status = request("/v1/status")
            if (
                contract.get("api_contract_version") != CONTRACT
                or contract.get("schema_version") != 1
                or not contract.get("self_identity")
            ):
                raise ChatDataError("SELF_IDENTITY_UNSUPPORTED", 409)
            if (
                status.get("dataset_id") != info["dataset"]
                or self.cache.get("dataset") != info["dataset"]
            ):
                raise ChatDataError("DATASET_MISMATCH", 409)
            for _ in range(max_batches):
                with self.cache.connect() as db:
                    db.execute(
                        "UPDATE chat_identity_refresh_items SET state='failed',error='RETRY_EXHAUSTED' WHERE job=? AND state='pending' AND attempts>=3",
                        (job,),
                    )
                    items = [
                        dict(r)
                        for r in db.execute(
                            "SELECT * FROM chat_identity_refresh_items WHERE job=? AND state='pending' ORDER BY id LIMIT 100",
                            (job,),
                        )
                    ]
                if not items:
                    break
                with self.cache.connect() as db:
                    db.executemany(
                        "UPDATE chat_identity_refresh_items SET attempts=attempts+1 WHERE job=? AND id=?",
                        [(job, x["id"]) for x in items],
                    )
                try:
                    response = request(
                        "/v1/records",
                        {
                            "items": [
                                {"record_id": x["id"], "record_revision": x["revision"]}
                                for x in items
                            ]
                        },
                    )
                    results = response.get("items")
                    expected = {(x["id"], x["revision"]) for x in items}
                    if (
                        not isinstance(results, list)
                        or len(results) != len(items)
                        or any(
                            not isinstance(x, dict)
                            or not isinstance(x.get("record_id"), str)
                            or type(x.get("record_revision")) is not int
                            for x in results
                        )
                        or {
                            (x.get("record_id"), x.get("record_revision"))
                            for x in results
                        }
                        != expected
                    ):
                        raise ChatDataError("INVALID_IDENTITY_RESPONSE", 502)
                except ChatDataError as e:
                    if (
                        e.code
                        in {
                            "MAC_OFFLINE",
                            "SERVICE_BUSY",
                            "QUERY_TIMEOUT",
                            "INITIALIZING",
                        }
                        or e.status == 503
                    ):
                        value["error"] = e.code
                        if self.stop.wait(0.2):
                            break
                        continue
                    raise
                for result in results:
                    error = None
                    try:
                        if result.get("status") != "found":
                            raise ChatDataError(
                                {
                                    "not_found": "REMOTE_NOT_FOUND",
                                    "revision_unavailable": "REMOTE_REVISION_UNAVAILABLE",
                                }.get(result.get("status"), "INVALID_RECORD_STATUS"),
                                409,
                            )
                        record = result.get("record")
                        if (
                            not isinstance(record, dict)
                            or record.get("record_id") != result["record_id"]
                            or record.get("record_revision")
                            != result["record_revision"]
                        ):
                            raise ChatDataError("IDENTITY_RECORD_MISMATCH", 502)
                        value["changed"] += self.cache.update_identity(
                            info["dataset"], record
                        )
                    except ChatDataError as e:
                        error = e.code
                    with self.cache.connect() as db:
                        db.execute(
                            "UPDATE chat_identity_refresh_items SET state=?,error=? WHERE job=? AND id=?",
                            (
                                "failed" if error else "done",
                                error,
                                job,
                                result["record_id"],
                            ),
                        )
                checkpoint()
                if progress:
                    progress(self.status(job))
            current = self.status(job)
            value["state"] = (
                "paused"
                if current["remaining"]
                else "partial"
                if current["failed"] or value["outside_budget"]
                else "completed"
            )
            if not current["remaining"]:
                value["error"] = None
        except (KeyboardInterrupt, ChatDataError) as e:
            value["state"] = "paused"
            value["error"] = getattr(e, "code", "INTERRUPTED")
        finally:
            value["elapsed"] = charged + time.monotonic() - start
            value["active_since"] = None
            self._save(job, value)
        return self.status(job)
