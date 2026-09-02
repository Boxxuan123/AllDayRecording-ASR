from __future__ import annotations
import threading
from typing import Any
from allday_asr.v3.domain.processing import (
    ProcessingClaim,
)
from allday_asr.v3.ports.processing import (
    ProcessingStageAdapter,
    StageExecutionContext,
    StageExecutionControl,
    StageCancellationRequested,
)

from .processing_service import DurableProcessingService


class DurableProcessingWorker:
    def __init__(
        self,
        service: DurableProcessingService,
        adapter: ProcessingStageAdapter,
        *,
        worker_id: str,
        config: dict[str, Any] | None = None,
        heartbeat_interval_seconds: float | None = None,
    ) -> None:
        if not worker_id:
            raise ValueError("worker id is required")
        selected_interval = (
            max(1.0, service.lease_seconds / 3.0)
            if heartbeat_interval_seconds is None
            else heartbeat_interval_seconds
        )
        if selected_interval <= 0 or selected_interval >= service.lease_seconds:
            raise ValueError("worker heartbeat interval must be within the lease")
        self.service = service
        self.adapter = adapter
        self.worker_id = worker_id
        self.config = dict(config or {})
        self.heartbeat_interval_seconds = selected_interval

    def run_once(self) -> bool:
        self.service.recover_expired()
        claim = self.service.claim(self.worker_id, self.config)
        if claim is None:
            return False
        control = _WorkerControl(self.service, claim)
        try:
            if control.heartbeat(claim.resume_checkpoint):
                self.service.cancel_claim(claim, "cancelled before stage execution")
                return True
            lease_heartbeat = _LeaseHeartbeat(control, self.heartbeat_interval_seconds)
            lease_heartbeat.start()
            try:
                result = self.adapter.execute(
                    StageExecutionContext(
                        claim=claim,
                        prior_artifacts=self.service.prior_artifacts(claim.run.run_id),
                    ),
                    control,
                )
            finally:
                lease_heartbeat.stop()
            lease_heartbeat.raise_if_failed()
            if control.heartbeat(result.checkpoint):
                self.service.cancel_claim(claim, "cancelled at safe checkpoint")
            else:
                self.service.complete(claim, result)
        except StageCancellationRequested as exc:
            self.service.cancel_claim(claim, str(exc))
        except Exception as exc:
            try:
                if control.heartbeat():
                    self.service.cancel_claim(
                        claim, "cancelled after stage execution stopped"
                    )
                else:
                    self.service.fail(
                        claim,
                        repr(exc),
                        retryable=not isinstance(exc, (TypeError, ValueError)),
                        log_summary=str(exc),
                    )
            except RuntimeError:
                recovered = self.service.recover_expired()
                if claim.job.job_id not in recovered:
                    raise
        return True


class _WorkerControl(StageExecutionControl):
    def __init__(
        self, service: DurableProcessingService, claim: ProcessingClaim
    ) -> None:
        self.service = service
        self.claim = claim
        self._cancelled = False
        self._lock = threading.Lock()

    def heartbeat(self, checkpoint: dict[str, Any] | None = None) -> bool:
        with self._lock:
            self._cancelled = (
                self.service.heartbeat(self.claim, checkpoint) or self._cancelled
            )
            return self._cancelled

    def cancellation_requested(self) -> bool:
        return self._cancelled


class _LeaseHeartbeat:
    def __init__(self, control: _WorkerControl, interval_seconds: float) -> None:
        self.control = control
        self.interval_seconds = interval_seconds
        self._stopped = threading.Event()
        self._failure: Exception | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="allday-v3-worker-heartbeat",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        self._thread.join()

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise RuntimeError("worker lease heartbeat failed") from self._failure

    def _run(self) -> None:
        while not self._stopped.wait(self.interval_seconds):
            try:
                self.control.heartbeat()
            except Exception as exc:  # noqa: BLE001 - surface in the worker thread
                self._failure = exc
                return
