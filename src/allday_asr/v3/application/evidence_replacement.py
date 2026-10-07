"""The single application rule for changed canonical utterance evidence."""

from __future__ import annotations

from datetime import datetime

from allday_asr.v3.application.knowledge import cascade_derivations
from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.processing import ArtifactInvalidation
from allday_asr.v3.ports.repositories import UnitOfWork


def invalidate_replaced_evidence(
    uow: UnitOfWork,
    *,
    old_utterance_id: str,
    replacement_revision: int,
    now: datetime,
    reason: str = "utterance_revision_changed",
    preserve_event_meaning: bool = False,
) -> None:
    """Invalidate automatic derivatives in the same transaction as replacement.

    User confirmed task events are excluded from invalidation and recomputation.
    Their old dependency revision remains available for source review, while
    the reminder repository preserves the scheduled Calendar lifecycle.
    Daily projections retain their own date/generation reconciliation rules.
    """
    for artifact_id in uow.artifacts.dependent_ids("utterance", old_utterance_id):
        uow.artifacts.invalidate(
            ArtifactInvalidation(
                status_event_id=new_ulid(), artifact_id=artifact_id,
                status="stale", reason=reason, source_type="utterance",
                source_id=old_utterance_id,
                source_revision=replacement_revision, created_at=now,
            )
        )
    if not preserve_event_meaning:
        uow.reminders.invalidate_source_candidates(
            old_utterance_id, now.isoformat()
        )
    cascade_derivations(
        uow, source_type="utterance", source_id=old_utterance_id,
        source_revision=replacement_revision, reason=reason,
        preserve_events=preserve_event_meaning, now=now,
    )
