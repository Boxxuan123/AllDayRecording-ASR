"""A small durable annotation worker, separate from fact receipt latency."""

import logging
import threading
import time

from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.annotation_sample_planning import compute_plans

LOG = logging.getLogger(__name__)


class AnnotationSampleWorker:
    def __init__(self, people, *, maximum_run_seconds=3600, lease_renew_interval=30):
        self.people = people
        self.maximum_run_seconds = maximum_run_seconds
        self.lease_renew_interval = lease_renew_interval
        self.last_success = None
        self.last_error = None
        self.stop_event = threading.Event()
        self.thread = None
        self.lock = threading.Lock()

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.stop_event.clear()
            self.thread = threading.Thread(
                target=self._loop, name="annotation-samples", daemon=True
            )
            self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
            if self.thread.is_alive():
                self.last_error = 'shutdown_incomplete'

    def health(self):
        with self.people._uow_factory().reading() as uow:
            backlog = uow.people.sample_queue_health(time.time())
        running = self.thread is not None and self.thread.is_alive()
        return {**backlog, 'state': 'failed' if self.last_error else 'running' if running else 'stopped',
                'last_success': self.last_success, 'last_error': self.last_error}

    def _loop(self):
        failures = 0
        while not self.stop_event.wait(1):
            try:
                self.run_pending(limit=4)
                failures = 0
            except Exception:
                self.last_error = "worker_iteration_failed"
                from .failure_diagnostics import log_failure
                import sys
                log_failure(LOG, sys.exception(), worker="annotation_samples")
                failures += 1
                if failures >= 3:
                    return

    def run_pending(self, limit=20):
        if not self.lock.acquire(blocking=False):
            return []
        try:
            return self._run_pending(min(max(limit, 1), 100))
        finally:
            self.lock.release()

    def _run_pending(self, limit):
        if self.stop_event.is_set():
            return []
        service = self.people
        provider = service._provider
        model, version = provider.model, provider.model_version
        with service._uow_factory() as uow:
            uow.people.refresh_sample_model(model, version)
        results = []
        for _ in range(limit):
            if self.stop_event.is_set():
                break
            token = new_ulid()
            claim_started = time.perf_counter()
            with service._uow_factory() as uow:
                job = uow.people.claim_sample_job(time.time(), token)
                if not job:
                    break
                session_id = job["session_id"]
            LOG.debug(
                "sample job=%s claim_ms=%.1f",
                token,
                (time.perf_counter() - claim_started) * 1000,
            )
            started = time.perf_counter()
            renew_stop, lease_lost = threading.Event(), threading.Event()
            deadline = time.monotonic() + self.maximum_run_seconds

            def renew(current_job=job, owner=token, halted=renew_stop, lost=lease_lost, expires=deadline):
                while not halted.wait(self.lease_renew_interval):
                    if self.stop_event.is_set() or time.monotonic() >= expires:
                        lost.set()
                        return
                    try:
                        with service._uow_factory() as uow:
                            if not uow.people.renew_sample_job(current_job['session_id'], current_job['generation'], owner, time.time()):
                                lost.set()
                                return
                    except Exception:
                        lost.set()
                        return

            renew_thread = threading.Thread(target=renew, daemon=True)
            renew_thread.start()
            try:
                with service._uow_factory().reading() as uow:
                    snapshot = uow.people.annotation_sample_snapshot(session_id)
                loaded = time.perf_counter()
                plans, reasons = compute_plans(snapshot, model, version)
                planned = time.perf_counter()
                with service._uow_factory().reading() as uow:
                    missing = [
                        p for p in plans if not uow.people.sample_set_exists(p.key)
                    ]
                LOG.debug(
                    "sample job=%s read_ms=%.1f plan_ms=%.1f projections=%d facts=%d",
                    token,
                    (loaded - started) * 1000,
                    (planned - loaded) * 1000,
                    len(snapshot.projections),
                    len(snapshot.facts),
                )
            except Exception:
                renew_stop.set()
                renew_thread.join(timeout=2)
                with service._uow_factory() as uow:
                    uow.people.finish_sample_job(
                        session_id,
                        job["generation"],
                        token,
                        "retryable",
                        "snapshot_or_plan_failed",
                        time.time(),
                    )
                raise
            status, reason = "processed", ",".join(reasons)
            computed = {}
            try:
                for plan in missing:
                    # No SQLite transaction is held during audio/model work.
                    embeddings = provider.embed((plan.track,))
                    if len(embeddings) != 1:
                        status, reason = "insufficient", "embedding_unusable"
                        continue
                    embedding = embeddings[0]
                    expected = [
                        (w["media_id"], w["start_ms"], w["end_ms"])
                        for w in plan.windows
                    ]
                    actual = [
                        (c.media_id, c.start_ms, c.end_ms)
                        for c in embedding.representatives
                    ]
                    if actual != expected:
                        raise ValueError(
                            "embedding did not cover the selected original-audio windows"
                        )
                    if (embedding.model, embedding.model_version) != (model, version):
                        raise ValueError("embedding model changed")
                    computed[plan.key] = embedding
                if not plans:
                    status, reason = (
                        "not_applicable",
                        reason or "no_eligible_person_evidence",
                    )
            except FileNotFoundError:
                status, reason = "missing_audio", "audio_unavailable"
            except Exception as exc:
                from .failure_diagnostics import log_failure
                log_failure(LOG, exc, session_id=session_id, job_id=token)
                status, reason = "retryable", type(exc).__name__
            renew_stop.set()
            renew_thread.join(timeout=2)
            publish_started = time.perf_counter()
            with service._uow_factory() as uow:
                revision = uow.people.annotation_input_revision()
                lease = uow.people.sample_job(session_id)
                valid = (
                    not self.stop_event.is_set() and not lease_lost.is_set()
                    and time.monotonic() < deadline
                    and lease.get("token") == token
                    and lease.get("generation") == job["generation"]
                    and revision == snapshot.revision
                    and lease.get("lease_until", 0) >= time.time()
                    and (service._provider.model, service._provider.model_version)
                    == (model, version)
                )
                if not valid:
                    status, reason = "retryable", "source_or_model_changed"
                else:
                    for plan in plans:
                        # A person fact proposes enrollment; it never grants purity.
                        # This shadow queue cannot authorize legacy prototypes.
                        uow.people.register_sample_candidates(plan, service._now().isoformat())
                        if (
                            not uow.people.sample_set_exists(plan.key)
                            and plan.key in computed
                        ):
                            uow.people.record_sample_set(
                                plan,
                                computed[plan.key],
                                new_ulid(),
                                service._now().isoformat(),
                            )
                        if uow.people.sample_set_exists(
                            plan.key
                        ) and not uow.people.select_sample_set(plan):
                            status, reason = (
                                "not_applicable",
                                "historical_source_revoked",
                            )
                # Current selection and completion share a transaction; a crash
                # cannot leave a processed job without its selected result.
                uow.people.finish_sample_job(
                    session_id, job["generation"], token, status, reason, time.time()
                )
            LOG.debug(
                "sample job=%s publish_ms=%.1f status=%s",
                token,
                (time.perf_counter() - publish_started) * 1000,
                status,
            )
            self.last_success = time.time() if status == "processed" else self.last_success
            self.last_error = reason if status == "retryable" else None
            results.append(
                {"session_id": session_id, "status": status, "reason": reason}
            )
        return results
