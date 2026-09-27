"""A small durable annotation worker, separate from fact receipt latency."""

import logging
import threading
import time

from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.adapters.sqlite.annotation_sample_plan import compute_plans
from allday_asr.v3.adapters.sqlite.annotation_sample_snapshot import load_snapshot

LOG = logging.getLogger(__name__)


class AnnotationSampleWorker:
    def __init__(self, people):
        self.people = people
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

    def _loop(self):
        while not self.stop_event.wait(1):
            try:
                self.run_pending(limit=4)
            except Exception:
                LOG.exception("annotation sample worker iteration failed")

    def run_pending(self, limit=20):
        if not self.lock.acquire(blocking=False):
            return []
        try:
            return self._run_pending(min(max(limit, 1), 100))
        finally:
            self.lock.release()

    def _run_pending(self, limit):
        service = self.people
        provider = service._provider
        model, version = provider.model, provider.model_version
        with service._uow_factory() as uow:
            uow.people.refresh_sample_model(model, version)
        results = []
        for _ in range(limit):
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
            try:
                with service._uow_factory().reading() as uow:
                    snapshot = load_snapshot(uow.people.connection, session_id)
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
            except FileNotFoundError as exc:
                status, reason = "missing_audio", str(exc)
            except Exception as exc:
                status, reason = "retryable", str(exc)
            publish_started = time.perf_counter()
            with service._uow_factory() as uow:
                revision = uow.people.connection.execute(
                    "SELECT revision FROM annotation_input_revision WHERE singleton=1"
                ).fetchone()[0]
                lease = uow.people.sample_job(session_id)
                valid = (
                    lease.get("token") == token
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
            results.append(
                {"session_id": session_id, "status": status, "reason": reason}
            )
        return results
