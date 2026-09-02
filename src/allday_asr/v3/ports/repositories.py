from __future__ import annotations

from datetime import datetime
from types import TracebackType
from typing import Any, Protocol, Self

from allday_asr.v3.domain.models import (
    Artifact,
    AudioAsset,
    AudioReplica,
    CaptureSegment,
    ChangeEvent,
    CorrectionOperation,
    Device,
    ProcessingRun,
    RecordingSession,
    SessionManifest,
)
from allday_asr.v3.domain.device_sync import (
    ClientOperation,
    ClientOperationRecord,
    DeviceCredential,
    DeviceScope,
    OperationReceipt,
    PairingRecord,
)
from allday_asr.v3.domain.processing import (
    ArtifactInvalidation,
    BackupEvidence,
    ProcessingClaim,
    ProcessingJob,
    ProcessingSnapshot,
    SpeakerTrack,
    StageRun,
    Utterance,
)
from allday_asr.v3.domain.knowledge import (
    DerivationDependency,
    EventCurrentState,
    EventOperation,
    EvidenceSpan,
    GenerationRecord,
    InvalidationEvent,
    MemoryRecord,
    RecomputeRequest,
    StructuredProposal,
)
from allday_asr.v3.domain.reminders import (
    ReminderCandidate,
    ReminderFeedback,
    ReminderSchedule,
)
from allday_asr.v3.ports.speaker_embeddings import SpeakerTrackInput


class RecordingCatalogRepository(Protocol):
    def add_session(self, session: RecordingSession) -> bool: ...
    def add_asset(self, asset: AudioAsset) -> bool: ...
    def add_replica(self, replica: AudioReplica) -> bool: ...
    def add_segment(self, segment: CaptureSegment) -> bool: ...
    def add_manifest(self, manifest: SessionManifest) -> bool: ...
    def find_session_by_legacy_ref(
        self, legacy_ref: str
    ) -> RecordingSession | None: ...
    def find_session_by_manifest_sha256(
        self, sha256: str
    ) -> RecordingSession | None: ...
    def tombstone_duplicate_session(
        self,
        session_id: str,
        canonical_session_id: str,
        tombstoned_at: datetime,
    ) -> int | None: ...
    def find_asset_by_sha256(self, sha256: str) -> AudioAsset | None: ...
    def get_session(self, session_id: str) -> RecordingSession: ...


class DeviceRepository(Protocol):
    def add(self, device: Device) -> bool: ...


class ProcessingRunRepository(Protocol):
    def add(self, run: ProcessingRun) -> bool: ...
    def find_by_legacy_ref(self, legacy_ref: str) -> ProcessingRun | None: ...
    def get(self, run_id: str) -> ProcessingRun: ...


class ArtifactRepository(Protocol):
    def add(self, artifact: Artifact) -> bool: ...
    def list_active_for_run(self, run_id: str) -> tuple[Artifact, ...]: ...
    def add_dependency(
        self, artifact_id: str, input_type: str, input_id: str, input_revision: int
    ) -> bool: ...
    def dependent_ids(self, input_type: str, input_id: str) -> tuple[str, ...]: ...
    def invalidate(self, event: ArtifactInvalidation) -> bool: ...


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


class ChangeLogRepository(Protocol):
    def append(
        self,
        resource_type: str,
        resource_id: str,
        revision: int,
        operation: str,
        payload: dict[str, Any] | None,
    ) -> int: ...

    def list_after(self, sequence: int, limit: int) -> tuple[ChangeEvent, ...]: ...

    def latest(self, resource_type: str, resource_id: str) -> ChangeEvent | None: ...


class DeviceTrustRepository(Protocol):
    def enroll(self, credential: DeviceCredential, pairing: PairingRecord) -> bool: ...

    def find_by_key_id(self, key_id: str) -> DeviceCredential | None: ...

    def authorize(
        self, key_id: str, required_scopes: tuple[DeviceScope, ...]
    ) -> DeviceCredential: ...

    def mark_used(self, key_id: str) -> None: ...

    def revoke(self, key_id: str, revoked_at: str) -> bool: ...


class MobileSyncRepository(Protocol):
    def find_operation(self, operation_id: str) -> ClientOperationRecord | None: ...

    def record_operation(
        self,
        device_id: str,
        operation: ClientOperation,
        payload_sha256: str,
        receipt: OperationReceipt,
    ) -> bool: ...

    def acknowledge_cursor(
        self, device_id: str, projection_version: int, sequence: int
    ) -> None: ...


class AuditRepository(Protocol):
    def append(
        self,
        action: str,
        actor: str,
        target_type: str,
        target_id: str,
        details: dict[str, Any],
        *,
        legacy_ref: str | None = None,
    ) -> bool: ...


class IdempotencyRepository(Protocol):
    def begin(self, key: str, command: str) -> bool: ...
    def complete(self, key: str, response: dict[str, Any]) -> None: ...
    def response(self, key: str) -> dict[str, Any] | None: ...


class TombstoneRepository(Protocol):
    def add(
        self,
        resource_type: str,
        resource_id: str,
        revision: int,
        reason: str | None,
    ) -> bool: ...


class DesktopReadRepository(Protocol):
    def overview(self) -> dict[str, Any]: ...
    def list_sessions(
        self,
        before_captured_start: str | None,
        before_session_id: str | None,
        limit: int,
    ) -> tuple[dict[str, Any], ...]: ...
    def session_detail(self, session_id: str) -> dict[str, Any]: ...
    def list_processing_jobs(
        self, status: str | None, limit: int
    ) -> tuple[dict[str, Any], ...]: ...
    def list_reviews(self, limit: int) -> tuple[dict[str, Any], ...]: ...
    def list_devices(self) -> tuple[dict[str, Any], ...]: ...
    def data_health(self) -> dict[str, Any]: ...
    def media(self, media_id: str) -> dict[str, Any]: ...
    def processing_events(
        self, after_sequence: int, limit: int
    ) -> tuple[dict[str, Any], ...]: ...


class KnowledgeRepository(Protocol):
    def unmaterialized_evidence(
        self, session_id: str
    ) -> tuple[dict[str, Any], ...]: ...
    def add_evidence_span(self, span: EvidenceSpan) -> bool: ...
    def list_evidence(self, session_id: str) -> tuple[dict[str, Any], ...]: ...
    def next_generation_number(
        self,
        layer: str,
        producer: str,
        producer_version: str,
        model: str,
        prompt_version: str,
        extractor_version: str,
        input_sha256: str,
    ) -> int: ...
    def add_generation(self, generation: GenerationRecord) -> bool: ...
    def get_generation(self, generation_id: str) -> GenerationRecord: ...
    def complete_generation(
        self, generation_id: str, status: str, completed_at: str, error: str | None
    ) -> None: ...
    def add_proposal(self, proposal: StructuredProposal) -> bool: ...
    def get_proposal(self, proposal_id: str) -> StructuredProposal: ...
    def list_proposals(
        self, status: str | None, limit: int
    ) -> tuple[dict[str, Any], ...]: ...
    def resolve_proposal(
        self,
        proposal_id: str,
        status: str,
        resolved_at: str,
        resolved_by: str,
        reason: str | None,
    ) -> None: ...
    def utterance_revisions(
        self, utterance_ids: tuple[str, ...]
    ) -> dict[str, tuple[str, int]]: ...
    def utterance_facts(
        self, utterance_ids: tuple[str, ...]
    ) -> dict[str, dict[str, Any]]: ...
    def get_event(self, event_id: str) -> EventCurrentState | None: ...
    def add_event_operation(self, operation: EventOperation) -> bool: ...
    def put_event_state(
        self, state: EventCurrentState, expected_revision: int
    ) -> None: ...
    def list_events(self, session_id: str) -> tuple[dict[str, Any], ...]: ...
    def event_history(self, event_id: str) -> tuple[dict[str, Any], ...]: ...
    def next_memory_version(self, memory_id: str) -> int: ...
    def add_memory(self, memory: MemoryRecord) -> bool: ...
    def list_memories(
        self, session_id: str | None, subject_type: str | None, subject_id: str | None
    ) -> tuple[dict[str, Any], ...]: ...
    def add_evidence_link(
        self,
        link_id: str,
        subject_type: str,
        subject_id: str,
        subject_revision: int,
        evidence_type: str,
        evidence_id: str,
        evidence_revision: int,
        created_at: str,
    ) -> bool: ...


class DerivationRepository(Protocol):
    def add_dependency(self, dependency: DerivationDependency) -> bool: ...
    def dependent_closure(
        self, input_type: str, input_id: str
    ) -> tuple[tuple[str, str, int], ...]: ...
    def add_invalidation(self, event: InvalidationEvent) -> bool: ...
    def add_recompute_request(self, request: RecomputeRequest) -> bool: ...
    def complete_recompute_for_target(
        self,
        target_type: str,
        target_id: str,
        target_revision: int,
        generation_id: str,
        updated_at: str,
    ) -> int: ...
    def list_invalidations(
        self, target_type: str | None, target_id: str | None, limit: int
    ) -> tuple[dict[str, Any], ...]: ...
    def list_recompute_requests(
        self, status: str | None, limit: int
    ) -> tuple[dict[str, Any], ...]: ...


class ReminderRepository(Protocol):
    def add_candidate(self, candidate: ReminderCandidate) -> bool: ...
    def get_candidate(self, candidate_id: str) -> ReminderCandidate: ...
    def get_candidate_by_proposal(
        self, proposal_id: str
    ) -> ReminderCandidate | None: ...
    def list_candidates(
        self, status: str | None, limit: int
    ) -> tuple[dict[str, Any], ...]: ...
    def duplicate_for(
        self, dedup_key: str, *, exclude_candidate_id: str | None = None
    ) -> dict[str, str] | None: ...
    def resolve_candidate(
        self,
        candidate_id: str,
        status: str,
        matched_event_id: str | None,
        conflict_reason: str | None,
        resolved_at: str,
        resolved_by: str,
    ) -> None: ...
    def get_schedule(self, event_id: str) -> ReminderSchedule | None: ...
    def put_schedule(self, schedule: ReminderSchedule) -> None: ...
    def list_schedules(
        self,
        *,
        status: str | None,
        session_id: str | None,
        due_before: str | None,
        limit: int,
    ) -> tuple[dict[str, Any], ...]: ...
    def mark_delivered(self, event_id: str, delivered_at: str) -> None: ...
    def mark_stale(
        self, event_id: str, event_revision: int, updated_at: str
    ) -> bool: ...
    def add_feedback(self, feedback: ReminderFeedback) -> bool: ...
    def list_feedback(self, candidate_id: str) -> tuple[dict[str, Any], ...]: ...


class PeopleRepository(Protocol):
    def analysis_inputs(self, session_id: str) -> tuple[SpeakerTrackInput, ...]: ...
    def confirmed_enrollment_input(
        self,
        session_id: str,
        speaker_track_id: str,
        windows: tuple[tuple[int, int], ...],
    ) -> dict[str, Any]: ...
    def person_kind(self, person_id: str) -> str: ...
    def self_person_id(self) -> str | None: ...
    def start_run(
        self,
        run_id: str,
        session_id: str,
        model: str,
        model_version: str,
        policy: dict[str, Any],
        track_count: int,
        created_at: str,
    ) -> None: ...
    def finish_run(
        self, run_id: str, status: str, completed_at: str, error: str | None
    ) -> None: ...
    def cluster_vectors(
        self, model: str, model_version: str
    ) -> tuple[tuple[str, tuple[float, ...]], ...]: ...
    def person_vectors(
        self, model: str, model_version: str
    ) -> tuple[tuple[str, tuple[float, ...]], ...]: ...
    def identity_policies(self) -> dict[str, dict[str, Any]]: ...
    def identity_policy(self, person_id: str) -> dict[str, Any]: ...
    def add_identity_policy_revision(
        self, person_id: str, **values: Any
    ) -> dict[str, Any]: ...
    def unlinked_cluster_embeddings(
        self, session_id: str | None = None
    ) -> tuple[dict[str, Any], ...]: ...
    def set_cluster_suggestion(
        self,
        cluster_id: str,
        person_id: str | None,
        confidence: float | None,
        updated_at: str,
    ) -> None: ...
    def record_match_decision(self, **values: Any) -> None: ...
    def prototype_candidate(self, prototype_id: str) -> dict[str, Any]: ...
    def latest_prototype_review(
        self, prototype_id: str, person_id: str
    ) -> dict[str, Any] | None: ...
    def add_prototype_review(self, **values: Any) -> dict[str, Any]: ...
    def prototype_review_examples(self, person_id: str) -> dict[str, Any]: ...
    def list_review_candidates(
        self, person_id: str | None, status: str | None, limit: int
    ) -> tuple[dict[str, Any], ...]: ...
    def record_embedding(self, **values: Any) -> None: ...
    def list_people(self) -> tuple[dict[str, Any], ...]: ...
    def list_clusters(
        self, status: str | None, limit: int
    ) -> tuple[dict[str, Any], ...]: ...
    def cluster_detail(self, cluster_id: str) -> dict[str, Any]: ...
    def create_person(
        self, person_id: str, display_name: str, kind: str, created_at: str
    ) -> None: ...
    def import_person(
        self,
        person_id: str,
        display_name: str,
        kind: str,
        aliases: tuple[str, ...],
        relationship_labels: tuple[str, ...],
        actor: str,
        created_at: str,
    ) -> bool: ...
    def label_cluster(
        self,
        cluster_id: str,
        person_id: str,
        actor: str,
        operation_id: str,
        created_at: str,
        *,
        source: str = "human",
        confidence: float = 1.0,
        promote_candidates: bool = False,
    ) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]: ...
    def merge_clusters(
        self,
        source_cluster_ids: tuple[str, ...],
        target_cluster_id: str,
        actor: str,
        operation_id: str,
        created_at: str,
    ) -> None: ...
    def split_cluster(
        self,
        cluster_id: str,
        speaker_track_ids: tuple[str, ...],
        new_cluster_id: str,
        new_label: str,
        actor: str,
        operation_id: str,
        created_at: str,
    ) -> None: ...
    def ignore_cluster(
        self,
        cluster_id: str,
        reason: str,
        actor: str,
        operation_id: str,
        created_at: str,
    ) -> None: ...
    def undo(
        self, cluster_id: str, actor: str, undo_operation_id: str, created_at: str
    ) -> dict[str, Any]: ...
    def events_referencing(self, reference_id: str) -> tuple[dict[str, Any], ...]: ...
    def events_by_ids(
        self, event_ids: tuple[str, ...]
    ) -> tuple[dict[str, Any], ...]: ...
    def cluster_evidence_ids(
        self, cluster_id: str, session_id: str | None = None
    ) -> tuple[str, ...]: ...


class PersonMemoryRepository(Protocol):
    def profile(self, person_id: str) -> dict[str, Any]: ...
    def update_profile(
        self,
        person_id: str,
        display_name: str,
        aliases: tuple[str, ...],
        relationship_labels: tuple[str, ...],
        notes: str,
        actor: str,
        created_at: str,
    ) -> dict[str, Any]: ...
    def event_sources(self, person_id: str) -> tuple[dict[str, Any], ...]: ...
    def current_memory(self, memory_id: str) -> dict[str, Any]: ...
    def add_revision(self, **values: Any) -> dict[str, Any]: ...
    def person_detail(self, person_id: str, limit: int = 200) -> dict[str, Any]: ...
    def list_current(
        self, person_id: str, *, limit: int, include_inactive: bool
    ) -> tuple[dict[str, Any], ...]: ...
    def summary_counts(self) -> dict[str, dict[str, Any]]: ...
    def revise_status(
        self,
        memory_id: str,
        status: str,
        actor: str,
        operation_id: str,
        operation_kind: str,
        created_at: str,
    ) -> dict[str, Any]: ...
    def undo(
        self, memory_id: str, actor: str, operation_id: str, created_at: str
    ) -> dict[str, Any]: ...
    def reconcile_identity(
        self,
        cluster_id: str,
        old_person_id: str | None,
        new_person_id: str | None,
        actor: str,
        created_at: str,
    ) -> int: ...


class InsightRepository(Protocol):
    def event_sources(self) -> tuple[dict[str, Any], ...]: ...
    def person_interactions(self, person_id: str) -> tuple[dict[str, Any], ...]: ...
    def person_profile(self, person_id: str) -> dict[str, Any]: ...
    def next_daily_revision(self, summary_id: str) -> int: ...
    def add_daily(self, **values: Any) -> None: ...
    def daily(self, summary_id: str) -> dict[str, Any]: ...
    def list_daily(self, limit: int) -> tuple[dict[str, Any], ...]: ...
    def next_relationship_revision(self, report_id: str) -> int: ...
    def add_relationship(self, **values: Any) -> None: ...
    def relationship(self, report_id: str) -> dict[str, Any]: ...
    def list_relationships(
        self, person_id: str | None, limit: int
    ) -> tuple[dict[str, Any], ...]: ...
    def add_evidence(self, *values: Any) -> bool: ...
    def add_operation(self, *values: Any, **options: Any) -> None: ...
    def latest_relationship_operation(
        self, report_id: str
    ) -> dict[str, Any] | None: ...
    def evidence_revisions(
        self, event_ids: tuple[str, ...], utterance_ids: tuple[str, ...]
    ) -> tuple[dict[str, int], dict[str, int]]: ...


class UnitOfWork(Protocol):
    catalog: RecordingCatalogRepository
    devices: DeviceRepository
    processing_runs: ProcessingRunRepository
    processing: DurableProcessingRepository
    admission: AdmissionRepository
    artifacts: ArtifactRepository
    evidence: EvidenceProjectionRepository
    corrections: CorrectionRepository
    changes: ChangeLogRepository
    device_trust: DeviceTrustRepository
    mobile_sync: MobileSyncRepository
    audit: AuditRepository
    idempotency: IdempotencyRepository
    tombstones: TombstoneRepository
    desktop: DesktopReadRepository
    knowledge: KnowledgeRepository
    derivations: DerivationRepository
    reminders: ReminderRepository
    people: PeopleRepository
    person_memories: PersonMemoryRepository
    insights: InsightRepository

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


__all__ = [
    "ArtifactRepository",
    "AdmissionRepository",
    "AuditRepository",
    "ChangeLogRepository",
    "CorrectionRepository",
    "DeviceRepository",
    "DeviceTrustRepository",
    "DesktopReadRepository",
    "DurableProcessingRepository",
    "EvidenceProjectionRepository",
    "IdempotencyRepository",
    "InsightRepository",
    "KnowledgeRepository",
    "MobileSyncRepository",
    "ProcessingRunRepository",
    "RecordingCatalogRepository",
    "TombstoneRepository",
    "DerivationRepository",
    "ReminderRepository",
    "PeopleRepository",
    "PersonMemoryRepository",
    "UnitOfWork",
]
