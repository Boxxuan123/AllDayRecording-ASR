from __future__ import annotations
from typing import Any, Protocol
from allday_asr.v3.domain.models import (
    CorrectionOperation,
    ProcessingRun,
)
from allday_asr.v3.domain.processing import (
    BackupEvidence,
    ProcessingClaim,
    ProcessingJob,
    ProcessingSnapshot,
    SpeakerTrack,
    StageRun,
    Utterance,
)


class DurableProcessingRepository(Protocol):
    def find_effective_run(
        self, session_id: str, input_revision: int, pipeline_version: str
    ) -> ProcessingRun | None: ...
    def find_succeeded_run(
        self,
        session_id: str,
        input_revision: int,
        pipeline_version: str,
        config_digest: str,
    ) -> ProcessingRun | None: ...
    def add_graph(
        self,
        run: ProcessingRun,
        job: ProcessingJob,
        stages: tuple[StageRun, ...],
    ) -> None: ...
    def get_snapshot(self, job_id: str) -> ProcessingSnapshot: ...
    def get_snapshot_for_run(self, run_id: str) -> ProcessingSnapshot: ...
    def claim_next(
        self, worker_id: str, lease_seconds: int, config: dict[str, Any]
    ) -> ProcessingClaim | None: ...
    def heartbeat(
        self,
        claim: ProcessingClaim,
        lease_seconds: int,
        checkpoint: dict[str, Any] | None,
    ) -> bool: ...
    def complete_stage(
        self,
        claim: ProcessingClaim,
        checkpoint: dict[str, Any],
        log_summary: str,
        output: dict[str, Any],
    ) -> ProcessingSnapshot: ...
    def fail_stage(
        self,
        claim: ProcessingClaim,
        error: str,
        log_summary: str,
        retryable: bool,
    ) -> ProcessingSnapshot: ...
    def cancel_claim(
        self, claim: ProcessingClaim, reason: str
    ) -> ProcessingSnapshot: ...
    def request_cancel(self, job_id: str, reason: str) -> ProcessingSnapshot: ...
    def retry(self, job_id: str) -> ProcessingSnapshot: ...
    def recover_expired(self) -> tuple[str, ...]: ...


class AdmissionRepository(Protocol):
    def add_evidence(self, evidence: BackupEvidence) -> bool: ...
    def evaluate(self, session_id: str) -> tuple[bool, str | None]: ...
    def apply(self, session_id: str, admitted: bool, reason: str | None) -> int: ...


class EvidenceProjectionRepository(Protocol):
    def add_speaker_track(self, track: SpeakerTrack) -> bool: ...
    def add_utterance(self, utterance: Utterance) -> bool: ...
    def get_utterance(self, utterance_id: str) -> Utterance: ...
    def speaker_label(self, speaker_track_id: str | None) -> str | None: ...
    def revise_utterance(
        self,
        utterance_id: str,
        expected_revision: int,
        text: str,
        speaker_track_id: str | None,
        identity: str,
    ) -> Utterance: ...


class CorrectionRepository(Protocol):
    def add(self, correction: CorrectionOperation) -> bool: ...
    def list_for_target(
        self, target_type: str, target_id: str
    ) -> tuple[CorrectionOperation, ...]: ...
