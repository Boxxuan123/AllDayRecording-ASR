from __future__ import annotations
from datetime import datetime, timezone
from typing import Any
from allday_asr.v3.domain.models import (
    ChangeOperation,
)
from allday_asr.v3.domain.processing import (
    ProcessingSnapshot,
)
from allday_asr.v3.ports.repositories import UnitOfWork


def _publish_processing(uow: UnitOfWork, snapshot: ProcessingSnapshot) -> None:
    uow.changes.append(
        "processing_run",
        snapshot.run.run_id,
        snapshot.run.revision,
        ChangeOperation.UPSERT.value,
        _processing_projection(snapshot),
    )


def _processing_projection(snapshot: ProcessingSnapshot) -> dict[str, Any]:
    return {
        "run_id": snapshot.run.run_id,
        "session_id": snapshot.run.session_id,
        "pipeline_version": snapshot.run.pipeline_version,
        "input_revision": snapshot.run.input_revision,
        "revision": snapshot.run.revision,
        "status": snapshot.run.status.value,
        "current_stage": snapshot.run.current_stage,
        "progress": snapshot.run.progress,
        "completed_at": (
            snapshot.run.completed_at.astimezone(timezone.utc).isoformat()
            if snapshot.run.completed_at is not None
            else None
        ),
        "error": snapshot.run.error,
    }


def _session_projection(session: Any) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "captured_start": session.captured_start.astimezone(timezone.utc).isoformat(),
        "captured_end": (
            session.captured_end.astimezone(timezone.utc).isoformat()
            if session.captured_end is not None
            else None
        ),
        "timezone": session.timezone,
        "state": session.state.value,
        "revision": session.revision,
        "status_code": session.status_code,
        "current_stage": session.current_stage,
        "progress": session.progress,
        "blocking_reason": session.blocking_reason,
    }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
