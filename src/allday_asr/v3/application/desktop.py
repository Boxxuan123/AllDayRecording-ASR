from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from allday_asr.v3 import CONTRACT_VERSION
from allday_asr.v3.domain.processing import ProcessingSnapshot
from allday_asr.v3.ports.repositories import UnitOfWork


UnitOfWorkFactory = Callable[[], UnitOfWork]


@dataclass(frozen=True)
class SessionPage:
    items: tuple[dict[str, Any], ...]
    next_cursor: str | None

    def as_dict(self) -> dict[str, Any]:
        return {"items": list(self.items), "next_cursor": self.next_cursor}


class DesktopQueryService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        codex_reminders_enabled: bool = False,
        codex_insights_enabled: bool = False,
        codex_semantic_events_enabled: bool = False,
    ) -> None:
        self._uow_factory = uow_factory
        self._codex_reminders_enabled = codex_reminders_enabled
        self._codex_insights_enabled = codex_insights_enabled
        self._codex_semantic_events_enabled = codex_semantic_events_enabled

    def status(self) -> dict[str, Any]:
        return {
            "contract_version": CONTRACT_VERSION,
            "enabled": True,
            "state": "ready",
        }

    def overview(self) -> dict[str, Any]:
        with self._uow_factory() as uow:
            return uow.desktop.overview()

    def list_sessions(
        self, cursor: str | None = None, limit: int = 50
    ) -> SessionPage:
        if not 1 <= limit <= 500:
            raise ValueError("session page limit must be between 1 and 500")
        before_start, before_id = _decode_cursor(cursor)
        with self._uow_factory() as uow:
            values = uow.desktop.list_sessions(before_start, before_id, limit + 1)
        page = values[:limit]
        next_cursor = None
        if len(values) > limit and page:
            last = page[-1]
            next_cursor = _encode_cursor(
                str(last["captured_start"]), str(last["session_id"])
            )
        return SessionPage(page, next_cursor)

    def session_detail(self, session_id: str) -> dict[str, Any]:
        with self._uow_factory() as uow:
            return uow.desktop.session_detail(session_id)

    def list_processing_jobs(
        self, status: str | None = None, limit: int = 100
    ) -> tuple[dict[str, Any], ...]:
        if not 1 <= limit <= 500:
            raise ValueError("processing page limit must be between 1 and 500")
        with self._uow_factory() as uow:
            return uow.desktop.list_processing_jobs(status, limit)

    def list_reviews(self, limit: int = 100) -> tuple[dict[str, Any], ...]:
        if not 1 <= limit <= 500:
            raise ValueError("review page limit must be between 1 and 500")
        with self._uow_factory() as uow:
            return uow.desktop.list_reviews(limit)

    def list_devices(self) -> tuple[dict[str, Any], ...]:
        with self._uow_factory() as uow:
            return uow.desktop.list_devices()

    def data_health(self) -> dict[str, Any]:
        with self._uow_factory() as uow:
            return uow.desktop.data_health()

    def media(self, media_id: str) -> dict[str, Any]:
        with self._uow_factory() as uow:
            return uow.desktop.media(media_id)

    def processing_events(
        self, after_sequence: int, limit: int = 100
    ) -> tuple[dict[str, Any], ...]:
        if after_sequence < 0 or not 1 <= limit <= 500:
            raise ValueError("processing event range is invalid")
        with self._uow_factory() as uow:
            return uow.desktop.processing_events(after_sequence, limit)

    def settings(self) -> dict[str, Any]:
        return {
            "contract_version": CONTRACT_VERSION,
            "deployment": "local_only",
            "processing": {
                "durable_jobs": True,
                "backup_admission_required": True,
                "automatic_source_deletion": False,
            },
            "privacy": {
                "network_boundary": "loopback",
                "audio_cloud_upload": False,
                "transcript_cloud_processing": (
                    self._codex_reminders_enabled or self._codex_insights_enabled
                    or self._codex_semantic_events_enabled
                ),
            },
            "knowledge": {
                "three_layers": True,
                "model_write_boundary": "structured_proposals_only",
                "recursive_invalidation": True,
                "recompute_queue": True,
                "codex_event_generation_enabled": self._codex_semantic_events_enabled,
                "event_auto_accept_policy": "record_only_high_confidence",
            },
            "reminders": {
                "codex_enabled": self._codex_reminders_enabled,
                "codex_workspace": "isolated_empty_read_only",
                "model_write_boundary": "structured_candidates_only",
            },
            "speaker_identity": {
                "open_set": True,
                "unknown_is_legal": True,
                "audio_processing": "local_only",
                "stable_prototype_policy": "explicit_user_confirmation",
                "phone_projection": False,
            },
            "person_memory": {
                "cross_day": True,
                "evidence_required": True,
                "facts_and_inferences_separated": True,
                "versioned_corrections": True,
                "phone_projection": False,
            },
            "insights": {
                "codex_enabled": self._codex_insights_enabled,
                "codex_workspace": "isolated_empty_read_only",
                "source_layer": "events_not_prior_summaries",
                "objective_statistics": "program_verified",
                "model_output": "evidence_bound_language_only",
                "relationship_windows_days": [7, 30],
                "versioned_corrections": True,
                "phone_projection": False,
            },
        }

    def lab(self) -> dict[str, Any]:
        return {
            "enabled": False,
            "label": "实验室",
            "message": "实验与 benchmark 工具保留在独立 Legacy/Lab 入口。",
        }


def processing_snapshot_dict(snapshot: ProcessingSnapshot) -> dict[str, Any]:
    return {
        "job": {
            "job_id": snapshot.job.job_id,
            "run_id": snapshot.job.run_id,
            "session_id": snapshot.run.session_id,
            "pipeline_version": snapshot.run.pipeline_version,
            "revision": snapshot.run.revision,
            "status": snapshot.job.status,
            "priority": snapshot.job.priority,
            "current_stage": snapshot.run.current_stage,
            "progress": snapshot.run.progress,
            "stage_count": len(snapshot.stages),
            "completed_stage_count": sum(
                stage.status.value == "succeeded" for stage in snapshot.stages
            ),
            "error": snapshot.job.error,
            "created_at": snapshot.job.created_at.isoformat(),
            "updated_at": snapshot.job.updated_at.isoformat(),
            "completed_at": (
                snapshot.job.completed_at.isoformat()
                if snapshot.job.completed_at is not None
                else None
            ),
        },
        "run": {
            "run_id": snapshot.run.run_id,
            "session_id": snapshot.run.session_id,
            "pipeline_version": snapshot.run.pipeline_version,
            "input_revision": snapshot.run.input_revision,
            "revision": snapshot.run.revision,
            "status": snapshot.run.status.value,
            "current_stage": snapshot.run.current_stage,
            "progress": snapshot.run.progress,
            "error": snapshot.run.error,
            "created_at": snapshot.run.created_at.isoformat(),
            "updated_at": snapshot.run.updated_at.isoformat(),
        },
        "stages": [
            {
                "stage_run_id": stage.stage_run_id,
                "stage": stage.stage,
                "ordinal": stage.ordinal,
                "optional": stage.optional,
                "status": stage.status.value,
                "progress": stage.progress,
                "output": stage.output,
                "error": stage.error,
            }
            for stage in snapshot.stages
        ],
        "attempts": [
            {
                "attempt_id": attempt.attempt_id,
                "stage_run_id": attempt.stage_run_id,
                "attempt_number": attempt.attempt_number,
                "status": attempt.status.value,
                "worker_id": attempt.worker_id,
                "config": attempt.config,
                "checkpoint": attempt.checkpoint,
                "log_summary": attempt.log_summary,
                "error": attempt.error,
                "started_at": attempt.started_at.isoformat(),
                "completed_at": (
                    attempt.completed_at.isoformat()
                    if attempt.completed_at is not None
                    else None
                ),
            }
            for attempt in snapshot.attempts
        ],
    }


def _encode_cursor(captured_start: str, session_id: str) -> str:
    payload = json.dumps(
        [captured_start, session_id], ensure_ascii=True, separators=(",", ":")
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: str | None) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if not value or len(value) > 2048:
        raise ValueError("session cursor is invalid")
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("session cursor is invalid") from exc
    if (
        not isinstance(decoded, list)
        or len(decoded) != 2
        or not all(isinstance(item, str) and item for item in decoded)
    ):
        raise ValueError("session cursor is invalid")
    return decoded[0], decoded[1]


__all__ = ["DesktopQueryService", "SessionPage", "processing_snapshot_dict"]
