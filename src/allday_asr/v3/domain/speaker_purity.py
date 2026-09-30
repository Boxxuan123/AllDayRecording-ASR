"""Audio purity is independent of the person fact attached to that audio."""

from dataclasses import dataclass
from enum import StrEnum


class PurityVerdict(StrEnum):
    CLEAN = "clean_single"
    MIXED = "mixed_overlap"
    BOUNDARY = "boundary_cross"
    WRONG = "wrong_primary"
    UNCERTAIN = "uncertain"
    UNREVIEWED = "unreviewed"


class ProfileMode(StrEnum):
    LEGACY = "legacy"
    PURITY_SHADOW = "purity_shadow"


@dataclass(frozen=True)
class SourcePurityEvidence:
    source_key: str
    verdict: PurityVerdict
    primary_person_id: str | None
    evidence_id: str
    conflicting: bool = False


def source_ineligibility(evidence, target_person_id, *, audio_available):
    if evidence is None:
        return "unreviewed"
    if evidence.conflicting:
        return "conflicting_review"
    if evidence.verdict != PurityVerdict.CLEAN:
        return str(evidence.verdict)
    if evidence.primary_person_id != target_person_id:
        return "wrong_primary" if evidence.primary_person_id else "uncertain"
    if not audio_available:
        return "source_audio_unavailable"
    return None


def is_source_eligible_for_clean_profile(
    evidence, target_person_id, *, audio_available
):
    return (
        source_ineligibility(
            evidence, target_person_id, audio_available=audio_available
        )
        is None
    )
