from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from allday_asr.v3.adapters.transfer.automation_state import (
    AutomaticWorkflowStateStore,
)


def merge_automatic_workflow_reviews(
    domain_items: Iterable[dict[str, Any]],
    store: AutomaticWorkflowStateStore,
    limit: int,
) -> tuple[dict[str, Any], ...]:
    values = list(domain_items)
    for state in store.statuses():
        if state.get("status") != "needs_attention":
            continue
        values.append(_review_item(state))
    priority = {"high": 0, "normal": 1}
    values.sort(
        key=lambda item: (
            priority.get(str(item["priority"]), 2),
            str(item["created_at"]),
            str(item["review_id"]),
        )
    )
    return tuple(values[:limit])


def _review_item(state: dict[str, Any]) -> dict[str, Any]:
    session_id = str(state["session_id"])
    stage = str(state.get("stage") or "automatic_workflow")
    created_at = str(state.get("created_at") or state.get("updated_at") or "")
    updated_at = str(state.get("updated_at") or created_at)
    return {
        "review_id": f"workflow_failure:{session_id}",
        "kind": "workflow_failure",
        "priority": "high",
        "source_id": session_id,
        "source_revision": None,
        "session_id": session_id,
        "person_id": None,
        "title": (
            "自动备份失败" if stage.startswith("backup") else "自动处理流程失败"
        ),
        "summary": str(state.get("error") or state.get("detail") or stage),
        "reason": "automatic_workflow_failed",
        "evidence_count": 0,
        "created_at": created_at,
        "updated_at": updated_at,
        "context": {
            "stage": stage,
            "error": state.get("error"),
            "attempt_count": int(state.get("attempt_count") or 0),
            "auto_retry_count": int(state.get("auto_retry_count") or 0),
            "max_auto_retries": int(state.get("max_auto_retries") or 0),
            "job_id": state.get("job_id"),
        },
    }


__all__ = ["merge_automatic_workflow_reviews"]
