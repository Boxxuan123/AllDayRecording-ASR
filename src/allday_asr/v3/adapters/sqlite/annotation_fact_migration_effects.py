"""Publish migrated projections and invalidate only changed human conclusions."""

from datetime import datetime, timezone
from types import SimpleNamespace
from allday_asr.v3.application.knowledge_support import cascade_derivations
from allday_asr.v3.contracts import utterance_dto
from .knowledge_repositories import SqliteDerivationRepository
from .reminder_repository import SqliteReminderRepository
from .repositories import SqliteChangeLogRepository


def publish_migrated_annotations(connection, repo, originals):
    now = datetime.now(timezone.utc)
    changes = SqliteChangeLogRepository(connection, now=lambda: now.isoformat())
    reminders = SqliteReminderRepository(connection)
    uow = SimpleNamespace(
        derivations=SqliteDerivationRepository(connection),
        reminders=reminders,
        changes=changes,
    )
    for original in originals:
        stored = repo.get_utterance(original.utterance_id)
        if stored.evidence == original.evidence:
            continue
        updated = repo.revise_utterance(
            stored.utterance_id,
            stored.revision,
            stored.text,
            stored.speaker_track_id,
            stored.identity.value,
            evidence=stored.evidence,
        )
        # revise_utterance normally represents a human edit; migration retains
        # historical stale status rather than reviving old ASR rows.
        connection.execute(
            "UPDATE utterances SET status=? WHERE utterance_id=?",
            (original.status, original.utterance_id),
        )
        updated = repo.get_utterance(updated.utterance_id)
        changes.append(
            "utterance",
            updated.utterance_id,
            updated.revision,
            "upsert",
            utterance_dto(
                updated,
                speaker_label=repo.speaker_label(updated.speaker_track_id),
                original_speaker_label=repo.speaker_label(
                    updated.original_speaker_track_id
                ),
            ),
        )
        if updated.evidence.get("annotation_outdated") or updated.evidence.get(
            "annotation_review"
        ):
            reminders.invalidate_source_candidates(
                updated.utterance_id, now.isoformat()
            )
            cascade_derivations(
                uow,
                source_type="utterance",
                source_id=updated.utterance_id,
                source_revision=updated.revision,
                reason="migrated_human_fact_superseded_or_conflicted",
                now=now,
            )
