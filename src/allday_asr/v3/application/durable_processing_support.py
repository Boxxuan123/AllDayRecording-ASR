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


def publish_superseded_utterances(uow: UnitOfWork, snapshot: ProcessingSnapshot) -> int:
    """Withdraw old phone projections only after replacement processing succeeds.

    Historical rows and correction operations stay in Core for review.
    """
    if snapshot.job.status != "succeeded":
        return 0
    connection = uow.catalog.connection
    previous = connection.execute(
        "SELECT u.utterance_id, u.revision, u.start_ms, u.end_ms, u.text, "
        "p.run_id, p.input_revision FROM utterances u "
        "JOIN processing_runs p ON p.run_id = u.run_id "
        "WHERE u.session_id = ? AND u.run_id != ? AND u.status = 'active' "
        "AND p.input_revision < ? ORDER BY u.start_ms, u.utterance_id",
        (snapshot.run.session_id, snapshot.run.run_id, snapshot.run.input_revision),
    ).fetchall()
    if not previous:
        return 0
    current = connection.execute(
        "SELECT utterance_id, start_ms, end_ms, text FROM utterances "
        "WHERE run_id = ? AND status = 'active' ORDER BY start_ms, utterance_id",
        (snapshot.run.run_id,),
    ).fetchall()
    if not current:
        return 0
    links = []
    for old in previous:
        candidates = [new for new in current if
                      min(old["end_ms"], new["end_ms"]) > max(old["start_ms"], new["start_ms"])]
        best = max(candidates, key=lambda new:
                   min(old["end_ms"], new["end_ms"]) -
                   max(old["start_ms"], new["start_ms"])) if candidates else None
        overlap = (min(old["end_ms"], best["end_ms"]) -
                   max(old["start_ms"], best["start_ms"])) if best else 0
        confirmed = bool(best and overlap >= 0.8 * (old["end_ms"] - old["start_ms"])
                         and old["text"] == best["text"])
        links.append({"old_utterance_id": old["utterance_id"],
                      "new_utterance_id": best["utterance_id"] if best else None,
                      "match": "confirmed" if confirmed else "needs_review"})
        connection.execute(
            "UPDATE utterances SET status='stale',revision=revision+1,updated_at=? "
            "WHERE utterance_id=? AND status='active'",
            (_utc_now().isoformat().replace("+00:00", "Z"), old["utterance_id"]),
        )
        uow.changes.append("utterance", old["utterance_id"], int(old["revision"]) + 1,
                           ChangeOperation.TOMBSTONE.value,
                           {"utterance_id": old["utterance_id"],
                            "session_id": snapshot.run.session_id,
                            "replacement_run_id": snapshot.run.run_id})
    uow.audit.append("processing.utterances.superseded", "system",
                     "processing_run", snapshot.run.run_id,
                     {"old_run_ids": sorted({old["run_id"] for old in previous}),
                      "replacement_run_id": snapshot.run.run_id, "links": links})
    return len(previous)


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
