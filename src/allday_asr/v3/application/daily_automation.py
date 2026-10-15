"""Bounded durable catch-up; generation uses the unchanged production service."""

import logging
import threading
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .file_lock import day_worker_lock
from allday_asr.v3.ports.daily_maintenance import (
    DailyMaintenanceRepository, DailyMaintenanceService, DailyInventory, DailyQueue, DailyOwnership,
)
from collections.abc import Callable


class DailyGenerationCoordinator:
    def __init__(
        self, database: DailyMaintenanceRepository, service_factory: Callable[[str], DailyMaintenanceService],
        version: str, *, model="gpt-5.6-luna", now=None,
        local_service: DailyMaintenanceService | None = None, remote_enabled=True,
        inventory: DailyInventory | None = None, queue: DailyQueue | None = None,
        ownership: DailyOwnership = day_worker_lock,
    ):
        self.database, self.service_factory, self.version = (
            database,
            service_factory,
            version,
        )
        self.local_service = local_service
        self.remote_enabled = remote_enabled
        self.last_success = None
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.inventory = inventory or database.daily_inventory(version, model=model, now=self.now)
        self.queue = queue or database.daily_generation_queue(self.now)
        self.ownership = ownership
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._lifecycle_lock = threading.Lock()
        self.last_trigger = None
        self.last_error = None
        self._current_service = None

    def catch_up(self, trigger="startup", *, origin="RECENT_DAILY"):
        with self.ownership(self.database.path) as acquired:
            if not acquired:
                return {'trigger': trigger, 'created_job_ids': [], 'items': []}
            return self._catch_up(trigger, origin=origin)

    def _catch_up(self, trigger, *, origin):
        rows = self.database.daily_dirty_ranges(limit=64)
        dates = set(self._recent_dates())
        consumed = []
        for dirty_id, first_text, last_text in rows:
            first = datetime.fromisoformat(first_text).date()
            last = datetime.fromisoformat(last_text).date()
            room = 14 - len(dates)
            if room <= 0:
                break
            stop = min(last, first + timedelta(days=room - 1))
            dates.update((first + timedelta(days=offset)).isoformat()
                         for offset in range((stop - first).days + 1))
            next_first = (stop + timedelta(days=1)).isoformat() if stop < last else None
            consumed.append((dirty_id, first_text, next_first))
        items = self.inventory.scan(include_today=True, only_dates=dates)
        if self.local_service is not None:
            for item in items:
                result = self.local_service.refresh_daily(item['date'], item['timezone'], allow_model=False)
                if result.get('semantic_status') in {'failed', 'retryable'}:
                    raise RuntimeError('daily local maintenance failed; dirty range retained')
                if result.get('semantic_status') == 'pending' and item['existing_summary_present'] and item['classification'] not in {'CURRENT_COMPLETE', 'CURRENT_COMPLETE_EMPTY'}:
                    self.local_service.mark_daily_stale(item['date'], item['timezone'], item['source_fingerprint'],
                                                       expected_revision=item['summary_revision'])
        recent = set(self._recent_dates())
        created = self.queue.observe([i for i in items if i['date'] in recent], origin=origin) if self.remote_enabled else []
        self.last_success = self.now().isoformat()
        self.database.acknowledge_daily_dirty_ranges(tuple(consumed))
        self.last_trigger = trigger
        return {"trigger": trigger, "created_job_ids": created, "items": items}

    def _recent_dates(self):
        today = self.now().astimezone(ZoneInfo(self.inventory.timezone)).date()
        return ((today - timedelta(days=1)).isoformat(), today.isoformat())

    def health(self):
        running = self._thread is not None and self._thread.is_alive()
        return {'state': 'failed' if self.last_error else 'running' if running else 'stopped',
                'last_success': self.last_success, 'last_error': self.last_error,
                'dirty_range_count': len(self.database.daily_dirty_ranges()),
                'pending_date_count': len(self.queue.pending_dates())}

    def full_reconcile(self, *, include_today=False):
        """Explicit read-only repair/diagnostic inventory, never the worker default."""
        return self.inventory.scan(include_today=include_today)

    def notify(self, trigger="receiver_sync"):
        # HTTP threads only wake a coalescing worker; never wait for inference.
        self.last_trigger = trigger
        self._wake.set()

    def start(self):
        with self._lifecycle_lock:
            if self._thread is not None:
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="daily-catch-up", daemon=True
            )
            self._thread.start()

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._current_service is not None:
            self._current_service.close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                self.last_error = "shutdown_incomplete"
            else:
                self._thread = None

    def _loop(self):
        trigger = "startup"
        while not self._stop.is_set():
            try:
                self.catch_up(trigger)
                while not self._stop.is_set() and self.run_once() is not None:
                    pass
                self.last_error = None
            except Exception as exc:
                self.last_error = type(exc).__name__
                from .failure_diagnostics import log_failure
                log_failure(logging.getLogger(__name__), exc, worker="daily")
                return  # No infinite crash loop; a restart can safely recover the lease.
            self._wake.wait(60)
            self._wake.clear()
            trigger = self.last_trigger or "completed_day_check"

    def run_once(self, *, allowed_dates=None, backfill=False):
        if not self.remote_enabled or self._stop.is_set():
            return None
        with self.ownership(self.database.path) as acquired:
            if not acquired:
                return None
            self.queue.recover_abandoned()
            dates = allowed_dates if allowed_dates is not None else (
                *self._recent_dates(), *self.queue.pending_dates())
            if not dates:
                return None
            items = self.inventory.scan(include_today=True, only_dates=dates)
            self.queue.observe(
                items, origin="HISTORICAL" if backfill else "RECENT_DAILY"
            )
            job = self.queue.claim(
                items, allowed_dates=allowed_dates, backfill=backfill
            )
            if job is None:
                return None
            stop_heartbeat = threading.Event()
            heartbeat_errors = []

            def heartbeat():
                while not stop_heartbeat.wait(30):
                    try:
                        self.queue.heartbeat(job)
                    except Exception as exc:
                        heartbeat_errors.append(exc)
                        return

            thread = threading.Thread(target=heartbeat, daemon=True)
            thread.start()
            service = None
            receipt_before = set()
            try:
                service = self.service_factory(job["local_date"])
                self._current_service = service
                if self._stop.is_set():
                    from allday_asr.v3.ports.processing import StageCancellationRequested
                    raise StageCancellationRequested('daily coordinator stopped')
                observation = getattr(service, 'daily_execution_observation', {})
                receipt_dir = observation.get('receipt_dir')
                if receipt_dir:
                    receipt_before = set(Path(receipt_dir).glob("*.result.json"))
                result = service.refresh_daily(job["local_date"], job["timezone"])
                from allday_asr.v3.domain.daily_semantics import SEMANTIC_VERSION

                success = (
                    result.get("objective", {}).get("semantic_status") == "complete"
                    and result["objective"].get("generation_version")
                    == SEMANTIC_VERSION
                )
                if heartbeat_errors:
                    raise heartbeat_errors[0]
                after = next((i for i in self.inventory.scan(
                    include_today=True, only_dates=(job["local_date"],))
                    if i["date"] == job["local_date"]), None)
                if after is None or after["source_fingerprint"] != job["source_fingerprint"]:
                    success = False
                    result = {"error": "daily_sources_changed"}
                status = self.queue.finish(
                    job,
                    result,
                    success=success,
                    retryable=result.get("retryable", True),
                    error=None
                    if success
                    else result.get("error", "generation_incomplete"),
                )
                receipts = (
                    sorted(
                        set(Path(receipt_dir).glob("*.result.json")) - receipt_before,
                        key=lambda p: p.stat().st_mtime,
                    )
                    if receipt_dir
                    else []
                )
                errors = [json.loads(p.read_text(encoding="utf-8")) for p in receipts]
                observation = getattr(service, 'daily_execution_observation', {})
                remote_starts = observation.get('remote_starts', 0)
                infra = None
                if (
                    not success
                    and errors
                    and all(e.get("status") != "RETURNED" for e in errors)
                ):
                    last = errors[-1]
                    message = last.get("error", "").lower()
                    infra = (
                        "timeout"
                        if last.get("status") == "TIMEOUT"
                        else "capacity"
                        if any(
                            w in message for w in ("capacity", "overload", "rate limit")
                        )
                        else "transport"
                        if any(
                            w in message for w in ("transport", "connection", "network")
                        )
                        else None
                    )
                return {
                    "date": job["local_date"],
                    "job_id": job["job_id"],
                    "attempt": job["attempts"],
                    "status": status,
                    "generation_completed": success,
                    "publish_completed": success,
                    "result": result,
                    "source_fingerprint": job["source_fingerprint"],
                    "remote_model_starts": remote_starts,
                    "cache_hits": observation.get('cache_hits', 0),
                    "first_request_infrastructure_failure": infra
                    if remote_starts <= 2
                    else None,
                }
            except Exception as exc:
                self.queue.finish(
                    job, {}, success=False, retryable=False, error=type(exc).__name__
                )
                raise  # Infrastructure/code errors stop the finite pass.
            finally:
                stop_heartbeat.set()
                thread.join()
                if service is not None:
                    service.close()
                self._current_service = None
