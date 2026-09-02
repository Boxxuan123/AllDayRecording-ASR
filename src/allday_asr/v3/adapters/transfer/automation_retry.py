from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


class AutomaticWorkflowRetryMixin:
    """Durable scheduling, restart recovery, and retry exhaustion behavior."""

    def _handle_failure(self, session_id: str, exc: Exception) -> None:
        current = self.status(session_id) or {}
        auto_retry_count = int(current.get("auto_retry_count") or 0)
        stage = str(current.get("stage") or "failed")
        if auto_retry_count < self.max_auto_retries:
            next_count = auto_retry_count + 1
            delay = self.retry_delays[next_count - 1]
            next_retry = datetime.now(timezone.utc) + timedelta(seconds=delay)
            self._progress(
                session_id,
                "retry_scheduled",
                stage,
                f"自动流水线失败，{_delay_label(delay)}后自动重试（{next_count}/{self.max_auto_retries}）",
                error=str(exc),
                error_type=type(exc).__name__,
                auto_retry_count=next_count,
                next_retry_at=next_retry.isoformat().replace("+00:00", "Z"),
                needs_manual_retry=False,
            )
        else:
            self._progress(
                session_id,
                "needs_attention",
                stage,
                "自动重试仍未成功，已进入审核收件箱，可点击重试",
                error=str(exc),
                error_type=type(exc).__name__,
                next_retry_at=None,
                needs_manual_retry=True,
            )
        print(f"[transfer:v3] {session_id} | workflow failure | {exc!r}")

    def _poll_persistent_work(self) -> None:
        for request in self._states.consume_retry_requests():
            session_id = str(request["session_id"])
            current = self.status(session_id) or {}
            if current.get("status") not in {"needs_attention", "retry_requested"}:
                continue
            self._queue_session(
                session_id,
                detail="已领取审核收件箱重试请求",
                auto_retry_count=0,
                manual_retry_count=int(current.get("manual_retry_count") or 0) + 1,
            )
        now = datetime.now(timezone.utc)
        for current in self._states.statuses():
            if current.get("status") != "retry_scheduled":
                continue
            next_retry_at = _parse_time(current.get("next_retry_at"))
            if next_retry_at is None or next_retry_at <= now:
                self._queue_session(
                    str(current["session_id"]),
                    detail="已到自动重试时间，重新进入流水线",
                )

    def _recover_pending(self) -> None:
        current_by_session = {
            str(value["session_id"]): value for value in self._states.statuses()
        }
        for session_id, current in current_by_session.items():
            status = current.get("status")
            if status in {"queued", "running"}:
                self._queue_session(session_id, detail="接收服务重启，自动恢复未完成流水线")
            elif status == "retry_scheduled":
                next_retry_at = _parse_time(current.get("next_retry_at"))
                if next_retry_at is None or next_retry_at <= datetime.now(timezone.utc):
                    self._queue_session(session_id, detail="接收服务重启，恢复到期的自动重试")

        with self.core.database.read() as connection:
            rows = connection.execute(
                """
                SELECT s.session_id FROM recording_sessions s
                WHERE s.tombstoned_at IS NULL
                  AND s.status_code = 'backup_required'
                  AND EXISTS (
                    SELECT 1 FROM session_manifests manifest
                    WHERE manifest.session_id = s.session_id
                  )
                  AND EXISTS (
                    SELECT 1 FROM capture_segments segment
                    WHERE segment.session_id = s.session_id
                  )
                ORDER BY s.created_at, s.session_id
                """
            ).fetchall()
        for row in rows:
            session_id = str(row["session_id"])
            current = current_by_session.get(session_id)
            if current is not None and current.get("status") in {
                "needs_attention",
                "retry_requested",
                "retry_scheduled",
            }:
                continue
            self._queue_session(
                session_id,
                detail="发现已上传但未备份的完整会话，自动补跑完整流程",
            )

    def _queue_session(self, session_id: str, *, detail: str, **values: Any) -> None:
        with self._lock:
            if self._closed or session_id in self._active:
                return
            current = self.status(session_id) or {
                "version": 1,
                "session_id": session_id,
                "created_at": _utc_now(),
                "attempt_count": 0,
                "auto_retry_count": 0,
            }
            current.update(
                {
                    "status": "queued",
                    "stage": str(current.get("stage") or "backup"),
                    "detail": detail,
                    "updated_at": _utc_now(),
                    "max_auto_retries": self.max_auto_retries,
                    "needs_manual_retry": False,
                    "next_retry_at": None,
                    **values,
                }
            )
            self._write(session_id, current)
            self._active.add(session_id)
            self._queue.put(session_id)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _delay_label(seconds: float) -> str:
    if seconds < 1:
        return "不到 1 秒"
    if seconds < 60:
        return f"{round(seconds)} 秒"
    return f"{round(seconds / 60)} 分钟"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = ["AutomaticWorkflowRetryMixin"]
