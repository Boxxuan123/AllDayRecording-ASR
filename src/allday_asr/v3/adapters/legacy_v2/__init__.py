from allday_asr.v3.adapters.legacy_v2.importer import LegacyV2Importer
from allday_asr.v3.adapters.legacy_v2.speaker_identity_backfill import (
    LegacySpeakerIdentityBackfill,
    LegacySpeakerIdentityBackfillResult,
)
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
from allday_asr.v3.application.legacy_import import ImportLegacyV2


def compose_legacy_v2_import(core) -> ImportLegacyV2:
    """Compose the explicit, one-way historical migration boundary."""

    return ImportLegacyV2(
        LegacyV2Importer(
            lambda: SqliteUnitOfWork(core.database),
            core.audio_store,
            core.artifact_store,
        )
    )


def compose_legacy_speaker_identity_backfill(core) -> LegacySpeakerIdentityBackfill:
    """Compose the final one-way migration of trusted V2 speaker labels."""

    return LegacySpeakerIdentityBackfill(
        lambda: SqliteUnitOfWork(core.database),
        core.people,
    )


__all__ = [
    "LegacySpeakerIdentityBackfill",
    "LegacySpeakerIdentityBackfillResult",
    "LegacyV2Importer",
    "compose_legacy_speaker_identity_backfill",
    "compose_legacy_v2_import",
]
