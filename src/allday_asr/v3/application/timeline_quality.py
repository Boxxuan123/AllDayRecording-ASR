from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from allday_asr.v3.domain.hashing import canonical_json_sha256
from allday_asr.v3.domain.timeline_quality import (
    TIMELINE_AUDIT_FORMAT,
    TimelineQualityDecision,
    evaluate_timeline_quality,
    parse_timeline_audit_document,
)


@dataclass(frozen=True)
class TimelineAuditSummary:
    accepted: bool
    blockers: tuple[str, ...]
    input_sha256: str
    receipt_sha256: str
    receipt_path: Path
    metrics: dict[str, Any]


def run_timeline_quality_audit(
    source_path: Path,
    *,
    receipt_path: Path | None = None,
) -> TimelineAuditSummary:
    try:
        value = json.loads(source_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("timeline audit input is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError("timeline audit input must be an object")
    session_id, completeness, seams, references, predictions = (
        parse_timeline_audit_document(value)
    )
    decision = evaluate_timeline_quality(
        seams, references, predictions, completeness
    )
    input_sha256 = canonical_json_sha256(value)
    body = _receipt_body(session_id, input_sha256, decision)
    receipt_sha256 = canonical_json_sha256(body)
    receipt = {**body, "receipt_sha256": receipt_sha256}
    selected_path = receipt_path or source_path.with_suffix(".receipt.json")
    _write_json_atomically(selected_path, receipt)
    return TimelineAuditSummary(
        accepted=decision.accepted,
        blockers=decision.blockers,
        input_sha256=input_sha256,
        receipt_sha256=receipt_sha256,
        receipt_path=selected_path,
        metrics=decision.metrics,
    )


def _receipt_body(
    session_id: str,
    input_sha256: str,
    decision: TimelineQualityDecision,
) -> dict[str, Any]:
    return {
        "format": TIMELINE_AUDIT_FORMAT,
        "receipt_version": 1,
        "session_id": session_id,
        "input_sha256": input_sha256,
        "policy": asdict(decision.policy),
        "decision": "accepted" if decision.accepted else "rejected",
        "blockers": list(decision.blockers),
        "metrics": decision.metrics,
    }


def _write_json_atomically(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


__all__ = ["TimelineAuditSummary", "run_timeline_quality_audit"]
