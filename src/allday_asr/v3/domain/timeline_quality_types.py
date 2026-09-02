from __future__ import annotations
from dataclasses import asdict, dataclass
from typing import Any

TIMELINE_AUDIT_FORMAT = "AllDayRecording V3.1 timeline seam audit v1"


TIMELINE_POLICY_VERSION = "v3.1-c.1"


def _validate_utterance_span(
    utterance_id: str, start_ms: int, end_ms: int, text: str, speaker_id: str
) -> None:
    if not utterance_id.strip() or not speaker_id.strip():
        raise ValueError("timeline utterance id and speaker are required")
    if start_ms < 0 or end_ms <= start_ms:
        raise ValueError("timeline utterance coordinates are invalid")
    if not isinstance(text, str):
        raise ValueError("timeline utterance text must be a string")


@dataclass(frozen=True)
class ChunkSeam:
    seam_id: str
    offset_ms: int
    left_chunk_id: str
    right_chunk_id: str
    reviewed: bool

    def __post_init__(self) -> None:
        if not self.seam_id.strip():
            raise ValueError("timeline seam id is required")
        if self.offset_ms < 1:
            raise ValueError("timeline seam offset must be positive")
        if not self.left_chunk_id.strip() or not self.right_chunk_id.strip():
            raise ValueError("timeline seam chunk ids are required")
        if self.left_chunk_id == self.right_chunk_id:
            raise ValueError("timeline seam must join two different chunks")


@dataclass(frozen=True)
class ReferenceUtterance:
    utterance_id: str
    start_ms: int
    end_ms: int
    text: str
    speaker_id: str
    has_overlap: bool

    def __post_init__(self) -> None:
        _validate_utterance_span(
            self.utterance_id, self.start_ms, self.end_ms, self.text, self.speaker_id
        )
        if not self.text.strip():
            raise ValueError("reference utterance text is required")


@dataclass(frozen=True)
class PredictedUtterance:
    utterance_id: str
    start_ms: int
    end_ms: int
    text: str
    speaker_id: str
    source_chunk_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_utterance_span(
            self.utterance_id, self.start_ms, self.end_ms, self.text, self.speaker_id
        )
        if not self.source_chunk_ids or any(
            not value.strip() for value in self.source_chunk_ids
        ):
            raise ValueError("prediction source chunk ids are required")
        if len(set(self.source_chunk_ids)) != len(self.source_chunk_ids):
            raise ValueError("prediction source chunk ids must be unique")


@dataclass(frozen=True)
class TimelineTruthCompleteness:
    alignment: str
    transcript: str
    speaker: str
    overlap: str

    def blockers(self) -> list[str]:
        return [
            f"{name}_truth_not_exhaustive"
            for name, value in asdict(self).items()
            if value != "exhaustive"
        ]


@dataclass(frozen=True)
class TimelineQualityPolicy:
    policy_version: str = TIMELINE_POLICY_VERSION
    seam_window_ms: int = 5_000
    min_reviewed_seams: int = 10
    min_reference_utterances: int = 30
    min_overlap_utterances: int = 5
    gross_boundary_error_ms: int = 1_500
    gross_utterance_cer: float = 0.50
    max_missed_reference_rate: float = 0.05
    max_extra_prediction_rate: float = 0.05
    max_gross_misalignment_rate: float = 0.05
    max_p95_boundary_error_ms: int = 750
    max_seam_cer: float = 0.20
    max_speaker_confusion_rate: float = 0.05
    max_speaker_continuity_error_rate: float = 0.05
    max_overlap_error_rate: float = 0.20

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise ValueError("timeline quality policy version is required")
        if (
            min(
                self.seam_window_ms,
                self.min_reviewed_seams,
                self.min_reference_utterances,
                self.min_overlap_utterances,
                self.gross_boundary_error_ms,
                self.max_p95_boundary_error_ms,
            )
            < 1
        ):
            raise ValueError("timeline quality policy counts must be positive")
        for value in (
            self.gross_utterance_cer,
            self.max_missed_reference_rate,
            self.max_extra_prediction_rate,
            self.max_gross_misalignment_rate,
            self.max_seam_cer,
            self.max_speaker_confusion_rate,
            self.max_speaker_continuity_error_rate,
            self.max_overlap_error_rate,
        ):
            if not 0 <= value <= 1:
                raise ValueError(
                    "timeline quality policy rates must be between 0 and 1"
                )


@dataclass(frozen=True)
class TimelineQualityDecision:
    accepted: bool
    blockers: tuple[str, ...]
    metrics: dict[str, Any]
    policy: TimelineQualityPolicy


@dataclass(frozen=True)
class _MatchedUtterance:
    seam_id: str
    seam_offset_ms: int
    reference: ReferenceUtterance
    prediction: PredictedUtterance
    overlap_ms: int
    start_error_ms: int
    end_error_ms: int
    edit_distance: int
    reference_characters: int

    @property
    def utterance_cer(self) -> float:
        return self.edit_distance / self.reference_characters
