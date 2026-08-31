from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from allday_asr.domain.hashing import canonical_json_sha256
from allday_asr.v3.contracts import UtteranceDto, utterance_dto
from allday_asr.v3.domain.ids import new_ulid, stable_ulid
from allday_asr.v3.domain.models import (
    Artifact,
    ChangeOperation,
    CorrectionOperation,
    ProcessingRun,
    ProcessingStatus,
)
from allday_asr.v3.domain.processing import (
    ArtifactInvalidation,
    BackupEvidence,
    BackupEvidenceStatus,
    DEFAULT_PROCESSING_STAGES,
    ProcessingClaim,
    ProcessingJob,
    ProcessingSnapshot,
    SpeakerTrack,
    StageDefinition,
    StageRun,
    StageStatus,
    Utterance,
)
from allday_asr.v3.ports.processing import (
    ProcessingStageAdapter,
    StageExecutionContext,
    StageExecutionControl,
    StageExecutionResult,
    StageCancellationRequested,
)
from allday_asr.v3.ports.repositories import UnitOfWork
from allday_asr.v3.ports.stores import ContentStore


UnitOfWorkFactory = Callable[[], UnitOfWork]
DateTimeClock = Callable[[], datetime]


@dataclass(frozen=True)
class SubmitProcessingCommand:
    session_id: str
    pipeline_version: str
    input_revision: int
    config: dict[str, Any]
    priority: int = 0
    stages: tuple[StageDefinition, ...] = DEFAULT_PROCESSING_STAGES


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


class AdmissionService:
    def __init__(
        self, uow_factory: UnitOfWorkFactory, *, now: DateTimeClock | None = None
    ) -> None:
        self._uow_factory = uow_factory
        self._now = now or _utc_now

    def record_verified_backup(
        self, command: RecordBackupEvidenceCommand
    ) -> bool:
        now = self._now()
        evidence = BackupEvidence(
            evidence_id=new_ulid(),
            session_id=command.session_id,
            provider=command.provider,
            storage_kind=command.storage_kind,
            digest=command.digest.lower(),
            status=BackupEvidenceStatus.VERIFIED,
            restore_checked_at=command.restore_checked_at,
            metadata=command.metadata,
            created_at=now,
        )
        with self._uow_factory() as uow:
            added = uow.admission.add_evidence(evidence)
            admitted, reason = uow.admission.evaluate(command.session_id)
            revision = uow.admission.apply(command.session_id, admitted, reason)
            session = uow.catalog.get_session(command.session_id)
            uow.changes.append(
                "recording_session",
                session.session_id,
                revision,
                ChangeOperation.UPSERT.value,
                _session_projection(session),
            )
            uow.audit.append(
                "session.admission.evaluated",
                "system",
                "recording_session",
                command.session_id,
                {
                    "admitted": admitted,
                    "blocking_reason": reason,
                    "backup_evidence_id": evidence.evidence_id,
                },
            )
        return added and admitted

    def evaluate(self, session_id: str) -> bool:
        with self._uow_factory() as uow:
            admitted, reason = uow.admission.evaluate(session_id)
            revision = uow.admission.apply(session_id, admitted, reason)
            session = uow.catalog.get_session(session_id)
            uow.changes.append(
                "recording_session",
                session_id,
                revision,
                ChangeOperation.UPSERT.value,
                _session_projection(session),
            )
        return admitted


class DurableProcessingService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        artifact_store: ContentStore,
        *,
        now: DateTimeClock | None = None,
        lease_seconds: int = 60,
    ) -> None:
        if lease_seconds < 5:
            raise ValueError("processing lease must be at least five seconds")
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._now = now or _utc_now
        self.lease_seconds = lease_seconds

    def submit(self, command: SubmitProcessingCommand) -> ProcessingSnapshot:
        if command.input_revision < 1 or not command.pipeline_version.strip():
            raise ValueError("processing command has invalid identity")
        if not command.stages or len({stage.name for stage in command.stages}) != len(
            command.stages
        ):
            raise ValueError("processing stage plan must be non-empty and unique")
        config_digest = canonical_json_sha256(command.config)
        now = self._now()
        with self._uow_factory() as uow:
            admitted, reason = uow.admission.evaluate(command.session_id)
            if not admitted:
                raise ValueError(f"session is not admitted for processing: {reason}")
            existing = uow.processing.find_effective_run(
                command.session_id,
                command.input_revision,
                command.pipeline_version,
            )
            if existing is not None:
                if existing.config_digest != config_digest:
                    raise ValueError(
                        "an effective processing run already exists with different config"
                    )
                return uow.processing.get_snapshot_for_run(existing.run_id)
            run_id = new_ulid()
            job_id = new_ulid()
            run = ProcessingRun(
                run_id=run_id,
                session_id=command.session_id,
                pipeline_version=command.pipeline_version,
                input_revision=command.input_revision,
                status=ProcessingStatus.QUEUED,
                config_digest=config_digest,
                current_stage=command.stages[0].name,
                progress=0.0,
                created_at=now,
                updated_at=now,
            )
            job = ProcessingJob(
                job_id=job_id,
                run_id=run_id,
                kind="session_processing",
                status=ProcessingStatus.QUEUED.value,
                priority=command.priority,
                request={
                    "session_id": command.session_id,
                    "pipeline_version": command.pipeline_version,
                    "input_revision": command.input_revision,
                    "config": command.config,
                    "stage_plan": [
                        {"name": stage.name, "optional": stage.optional}
                        for stage in command.stages
                    ],
                },
                available_at=now,
                created_at=now,
                updated_at=now,
            )
            stages = tuple(
                StageRun(
                    stage_run_id=stable_ulid("stage-run", run_id, definition.name),
                    run_id=run_id,
                    stage=definition.name,
                    ordinal=index,
                    optional=definition.optional,
                    status=StageStatus.PENDING,
                    progress=0.0,
                    created_at=now,
                    updated_at=now,
                )
                for index, definition in enumerate(command.stages)
            )
            uow.processing.add_graph(run, job, stages)
            snapshot = uow.processing.get_snapshot(job_id)
            _publish_processing(uow, snapshot)
            uow.audit.append(
                "processing.submitted",
                "system",
                "processing_job",
                job_id,
                {"run_id": run_id, "config_digest": config_digest},
            )
            return snapshot

    def get(self, job_id: str) -> ProcessingSnapshot:
        with self._uow_factory() as uow:
            return uow.processing.get_snapshot(job_id)

    def get_for_run(self, run_id: str) -> ProcessingSnapshot:
        with self._uow_factory() as uow:
            return uow.processing.get_snapshot_for_run(run_id)

    def claim(self, worker_id: str, config: dict[str, Any]) -> ProcessingClaim | None:
        with self._uow_factory() as uow:
            claim = uow.processing.claim_next(worker_id, self.lease_seconds, config)
            if claim is not None:
                _publish_processing(uow, uow.processing.get_snapshot(claim.job.job_id))
                uow.audit.append(
                    "processing.stage.claimed",
                    f"worker:{worker_id}",
                    "stage_run",
                    claim.stage.stage_run_id,
                    {
                        "attempt": claim.attempt.attempt_number,
                        "lease_id": claim.lease.lease_id,
                    },
                )
            return claim

    def heartbeat(
        self, claim: ProcessingClaim, checkpoint: dict[str, Any] | None = None
    ) -> bool:
        with self._uow_factory() as uow:
            return uow.processing.heartbeat(
                claim, self.lease_seconds, checkpoint
            )

    def complete(
        self, claim: ProcessingClaim, result: StageExecutionResult
    ) -> ProcessingSnapshot:
        stored_outputs = [
            (output, self._artifact_store.put_bytes(output.payload))
            for output in result.artifacts
        ]
        now = self._now()
        with self._uow_factory() as uow:
            created: dict[str, Artifact] = {}
            for output, stored in stored_outputs:
                artifact = Artifact(
                    artifact_id=stable_ulid(
                        "stage-artifact",
                        claim.attempt.attempt_id,
                        output.kind,
                        stored.sha256,
                    ),
                    run_id=claim.run.run_id,
                    kind=output.kind,
                    producer=output.producer,
                    producer_version=output.producer_version,
                    config_digest=claim.run.config_digest,
                    input_refs=output.input_refs,
                    storage_ref=stored.storage_key,
                    sha256=stored.sha256,
                    size_bytes=stored.size_bytes,
                    status="active",
                    metadata={
                        **output.metadata,
                        "stage": claim.stage.stage,
                        "attempt_id": claim.attempt.attempt_id,
                    },
                    created_at=now,
                )
                uow.artifacts.add(artifact)
                for dependency in output.dependencies:
                    uow.artifacts.add_dependency(
                        artifact.artifact_id,
                        dependency.input_type,
                        dependency.input_id,
                        dependency.input_revision,
                    )
                created[artifact.kind] = artifact

            source = created.get("v2_evidence_snapshot")
            if source is None:
                source = next(
                    (
                        artifact
                        for artifact in uow.artifacts.list_active_for_run(
                            claim.run.run_id
                        )
                        if artifact.kind == "v2_evidence_snapshot"
                    ),
                    None,
                )
            if result.utterances and source is None:
                raise RuntimeError("utterance projection has no immutable source artifact")

            tracks: dict[str, str] = {}
            if source is not None:
                for speaker in result.speakers:
                    track_id = stable_ulid(
                        "speaker-track", claim.run.run_id, speaker.label
                    )
                    uow.evidence.add_speaker_track(
                        SpeakerTrack(
                            speaker_track_id=track_id,
                            session_id=claim.run.session_id,
                            run_id=claim.run.run_id,
                            label=speaker.label,
                            source_artifact_id=source.artifact_id,
                            created_at=now,
                        )
                    )
                    tracks[speaker.label] = track_id
                for value in result.utterances:
                    utterance = Utterance(
                        utterance_id=stable_ulid(
                            "utterance", claim.run.run_id, value.ordinal
                        ),
                        session_id=claim.run.session_id,
                        run_id=claim.run.run_id,
                        source_artifact_id=source.artifact_id,
                        speaker_track_id=(
                            tracks.get(value.speaker_label)
                            if value.speaker_label is not None
                            else None
                        ),
                        ordinal=value.ordinal,
                        start_ms=value.start_ms,
                        end_ms=value.end_ms,
                        text=value.text,
                        evidence=value.evidence,
                        revision=1,
                        status="active",
                        created_at=now,
                        updated_at=now,
                    )
                    if uow.evidence.add_utterance(utterance):
                        uow.changes.append(
                            "utterance",
                            utterance.utterance_id,
                            utterance.revision,
                            ChangeOperation.UPSERT.value,
                            utterance_dto(
                                utterance, speaker_label=value.speaker_label
                            ),
                        )

            snapshot = uow.processing.complete_stage(
                claim,
                result.checkpoint,
                result.log_summary,
                {
                    "artifact_ids": [value.artifact_id for value in created.values()],
                    "utterance_count": len(result.utterances),
                },
            )
            _publish_processing(uow, snapshot)
            uow.audit.append(
                "processing.stage.succeeded",
                f"worker:{claim.attempt.worker_id}",
                "stage_run",
                claim.stage.stage_run_id,
                {
                    "attempt": claim.attempt.attempt_number,
                    "artifacts": list(created),
                    "utterance_count": len(result.utterances),
                },
            )
            return snapshot

    def fail(
        self,
        claim: ProcessingClaim,
        error: str,
        *,
        retryable: bool,
        log_summary: str = "",
    ) -> ProcessingSnapshot:
        with self._uow_factory() as uow:
            snapshot = uow.processing.fail_stage(
                claim, error, log_summary, retryable
            )
            _publish_processing(uow, snapshot)
            uow.audit.append(
                "processing.stage.failed",
                f"worker:{claim.attempt.worker_id}",
                "stage_run",
                claim.stage.stage_run_id,
                {"retryable": retryable, "error": error[:1000]},
            )
            return snapshot

    def cancel(self, job_id: str, reason: str) -> ProcessingSnapshot:
        with self._uow_factory() as uow:
            snapshot = uow.processing.request_cancel(job_id, reason)
            _publish_processing(uow, snapshot)
            uow.audit.append(
                "processing.cancel.requested",
                "user",
                "processing_job",
                job_id,
                {"reason": reason},
            )
            return snapshot

    def cancel_claim(
        self, claim: ProcessingClaim, reason: str
    ) -> ProcessingSnapshot:
        with self._uow_factory() as uow:
            snapshot = uow.processing.cancel_claim(claim, reason)
            _publish_processing(uow, snapshot)
            return snapshot

    def retry(self, job_id: str) -> ProcessingSnapshot:
        with self._uow_factory() as uow:
            snapshot = uow.processing.retry(job_id)
            _publish_processing(uow, snapshot)
            uow.audit.append(
                "processing.retry.queued",
                "user",
                "processing_job",
                job_id,
                {"attempts": len(snapshot.attempts)},
            )
            return snapshot

    def recover_expired(self) -> tuple[str, ...]:
        with self._uow_factory() as uow:
            recovered = uow.processing.recover_expired()
            for job_id in recovered:
                snapshot = uow.processing.get_snapshot(job_id)
                _publish_processing(uow, snapshot)
                uow.audit.append(
                    "processing.lease.lost",
                    "system",
                    "processing_job",
                    job_id,
                    {"run_id": snapshot.run.run_id},
                )
            return recovered

    def prior_artifacts(self, run_id: str) -> dict[str, tuple[Artifact, bytes]]:
        with self._uow_factory() as uow:
            artifacts = uow.artifacts.list_active_for_run(run_id)
        values: dict[str, tuple[Artifact, bytes]] = {}
        for artifact in artifacts:
            with self._artifact_store.open(artifact.storage_ref) as source:
                values[artifact.kind] = (artifact, source.read())
        return values


class CorrectionInvalidationService:
    def __init__(
        self, uow_factory: UnitOfWorkFactory, *, now: DateTimeClock | None = None
    ) -> None:
        self._uow_factory = uow_factory
        self._now = now or _utc_now

    def correct_utterance(self, command: CorrectUtteranceCommand) -> UtteranceDto:
        if not command.text.strip():
            raise ValueError("utterance text cannot be empty")
        now = self._now()
        with self._uow_factory() as uow:
            before = uow.evidence.get_utterance(command.utterance_id)
            if before.revision != command.expected_revision:
                raise ValueError("utterance revision conflict")
            correction_id = new_ulid()
            uow.corrections.add(
                CorrectionOperation(
                    correction_id=correction_id,
                    target_type="utterance",
                    target_id=command.utterance_id,
                    before_revision=before.revision,
                    patch={"text": command.text},
                    actor=command.actor,
                    created_at=now,
                )
            )
            updated = uow.evidence.revise_utterance(
                command.utterance_id, command.expected_revision, command.text
            )
            speaker_label = uow.evidence.speaker_label(updated.speaker_track_id)
            for artifact_id in uow.artifacts.dependent_ids(
                "utterance", command.utterance_id
            ):
                uow.artifacts.invalidate(
                    ArtifactInvalidation(
                        status_event_id=new_ulid(),
                        artifact_id=artifact_id,
                        status="stale",
                        reason="utterance_revision_changed",
                        source_type="utterance",
                        source_id=updated.utterance_id,
                        source_revision=updated.revision,
                        created_at=now,
                    )
                )
            uow.changes.append(
                "utterance",
                updated.utterance_id,
                updated.revision,
                ChangeOperation.UPSERT.value,
                utterance_dto(updated, speaker_label=speaker_label),
            )
            uow.audit.append(
                "utterance.corrected",
                command.actor,
                "utterance",
                updated.utterance_id,
                {"revision": updated.revision, "correction_id": correction_id},
            )
            return utterance_dto(updated, speaker_label=speaker_label)


class DurableProcessingWorker:
    def __init__(
        self,
        service: DurableProcessingService,
        adapter: ProcessingStageAdapter,
        *,
        worker_id: str,
        config: dict[str, Any] | None = None,
    ) -> None:
        if not worker_id:
            raise ValueError("worker id is required")
        self.service = service
        self.adapter = adapter
        self.worker_id = worker_id
        self.config = dict(config or {})

    def run_once(self) -> bool:
        self.service.recover_expired()
        claim = self.service.claim(self.worker_id, self.config)
        if claim is None:
            return False
        control = _WorkerControl(self.service, claim)
        try:
            if control.heartbeat(claim.resume_checkpoint):
                self.service.cancel_claim(claim, "cancelled before stage execution")
                return True
            result = self.adapter.execute(
                StageExecutionContext(
                    claim=claim,
                    prior_artifacts=self.service.prior_artifacts(claim.run.run_id),
                ),
                control,
            )
            if control.heartbeat(result.checkpoint):
                self.service.cancel_claim(claim, "cancelled at safe checkpoint")
            else:
                self.service.complete(claim, result)
        except StageCancellationRequested as exc:
            self.service.cancel_claim(claim, str(exc))
        except Exception as exc:
            try:
                if control.heartbeat():
                    self.service.cancel_claim(
                        claim, "cancelled after stage execution stopped"
                    )
                else:
                    self.service.fail(
                        claim,
                        repr(exc),
                        retryable=not isinstance(exc, (TypeError, ValueError)),
                        log_summary=str(exc),
                    )
            except RuntimeError:
                recovered = self.service.recover_expired()
                if claim.job.job_id not in recovered:
                    raise
        return True


class _WorkerControl(StageExecutionControl):
    def __init__(
        self, service: DurableProcessingService, claim: ProcessingClaim
    ) -> None:
        self.service = service
        self.claim = claim
        self._cancelled = False

    def heartbeat(self, checkpoint: dict[str, Any] | None = None) -> bool:
        self._cancelled = self.service.heartbeat(self.claim, checkpoint)
        return self._cancelled

    def cancellation_requested(self) -> bool:
        return self._cancelled


def _publish_processing(uow: UnitOfWork, snapshot: ProcessingSnapshot) -> None:
    uow.changes.append(
        "processing_run",
        snapshot.run.run_id,
        snapshot.run.revision,
        ChangeOperation.UPSERT.value,
        _processing_projection(snapshot),
    )


def _processing_projection(snapshot: ProcessingSnapshot) -> dict[str, Any]:
    return {
        "run_id": snapshot.run.run_id,
        "session_id": snapshot.run.session_id,
        "pipeline_version": snapshot.run.pipeline_version,
        "input_revision": snapshot.run.input_revision,
        "revision": snapshot.run.revision,
        "status": snapshot.run.status.value,
        "current_stage": snapshot.run.current_stage,
        "progress": snapshot.run.progress,
        "completed_at": (
            snapshot.run.completed_at.astimezone(timezone.utc).isoformat()
            if snapshot.run.completed_at is not None
            else None
        ),
        "error": snapshot.run.error,
    }


def _session_projection(session: Any) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "captured_start": session.captured_start.astimezone(timezone.utc).isoformat(),
        "captured_end": (
            session.captured_end.astimezone(timezone.utc).isoformat()
            if session.captured_end is not None
            else None
        ),
        "timezone": session.timezone,
        "state": session.state.value,
        "revision": session.revision,
        "status_code": session.status_code,
        "current_stage": session.current_stage,
        "progress": session.progress,
        "blocking_reason": session.blocking_reason,
    }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "AdmissionService",
    "CorrectUtteranceCommand",
    "CorrectionInvalidationService",
    "DurableProcessingService",
    "DurableProcessingWorker",
    "RecordBackupEvidenceCommand",
    "SubmitProcessingCommand",
]
