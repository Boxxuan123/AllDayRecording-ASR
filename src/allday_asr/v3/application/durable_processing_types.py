from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from allday_asr.v3.domain.identity import SelfIdentity
from allday_asr.v3.domain.processing import (
    DEFAULT_PROCESSING_STAGES,
    StageDefinition,
)
from allday_asr.v3.ports.repositories import UnitOfWork

UnitOfWorkFactory = Callable[[], UnitOfWork]


DateTimeClock = Callable[[], datetime]


class UtteranceRevisionConflict(ValueError):
    """The caller corrected an utterance revision that is no longer current."""


@dataclass(frozen=True)
class SubmitProcessingCommand:
    session_id: str
    pipeline_version: str
    input_revision: int
    config: dict[str, Any]
    priority: int = 0
    stages: tuple[StageDefinition, ...] = DEFAULT_PROCESSING_STAGES
    admission_mode: str = "production"
    force_reprocess: bool = False


@dataclass(frozen=True)
class RecordBackupEvidenceCommand:
    session_id: str
    provider: str
    storage_kind: str
    digest: str
    restore_checked_at: datetime
    metadata: dict[str, Any]


@dataclass(frozen=True)
class CorrectUtteranceCommand:
    utterance_id: str
    expected_revision: int
    text: str
    actor: str
    speaker_track_id: str | None = None
    change_speaker: bool = False
    identity: SelfIdentity | None = None
    change_identity: bool = False
