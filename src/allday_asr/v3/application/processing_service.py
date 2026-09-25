from __future__ import annotations
from datetime import timedelta
from typing import Any
from allday_asr.v3.domain.hashing import canonical_json_sha256
from allday_asr.v3.contracts import utterance_dto
from allday_asr.v3.domain.ids import new_ulid, stable_ulid
from allday_asr.v3.domain.identity import SelfIdentity
from allday_asr.v3.domain.models import (
    Artifact,
    ChangeOperation,
    ProcessingRun,
    ProcessingStatus,
)
from allday_asr.v3.domain.processing import (
    ProcessingClaim,
    ProcessingJob,
    ProcessingSnapshot,
    SpeakerTrack,
    StageRun,
    StageStatus,
    Utterance,
)
from allday_asr.v3.ports.processing import (
    StageExecutionResult,
)
from allday_asr.v3.ports.stores import ContentStore

from .durable_processing_types import (
    DateTimeClock,
    SubmitProcessingCommand,
    UnitOfWorkFactory,
)
from .durable_processing_support import (
    _publish_processing,
    _utc_now,
)


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
        if command.admission_mode not in {"production", "shadow"}:
            raise ValueError("processing admission mode must be production or shadow")
        effective_config = {
            **command.config,
            "admission_mode": command.admission_mode,
        }
        config_digest = canonical_json_sha256(effective_config)
        now = self._now()
        with self._uow_factory() as uow:
            admitted, reason = uow.admission.evaluate(command.session_id)
            if not admitted:
                session = uow.catalog.get_session(command.session_id)
                shadow_allowed = (
                    command.admission_mode == "shadow"
                    and session.state.value == "admission_pending"
                    and reason == "backup_restore_evidence_required"
                )
                if not shadow_allowed:
                    raise ValueError(
                        f"session is not admitted for processing: {reason}"
                    )
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
            if not command.force_reprocess:
                succeeded = uow.processing.find_succeeded_run(
                    command.session_id,
                    command.input_revision,
                    command.pipeline_version,
                    config_digest,
                )
                if succeeded is not None:
                    return uow.processing.get_snapshot_for_run(succeeded.run_id)
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
                    "admission_mode": command.admission_mode,
                    "config": effective_config,
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
            return uow.processing.heartbeat(claim, self.lease_seconds, checkpoint)

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

            source = created.get("v3_transcript_evidence")
            if source is None:
                source = next(
                    (
                        artifact
                        for artifact in uow.artifacts.list_active_for_run(
                            claim.run.run_id
                        )
                        if artifact.kind == "v3_transcript_evidence"
                    ),
                    None,
                )
            if result.utterances and source is None:
                raise RuntimeError(
                    "utterance projection has no immutable source artifact"
                )

            tracks: dict[str, str] = {}
            if source is not None:
                captured_start = uow.catalog.get_session(
                    claim.run.session_id
                ).captured_start
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
                    speaker_track_id = (
                        tracks.get(value.speaker_label)
                        if value.speaker_label is not None
                        else None
                    )
                    utterance = Utterance(
                        utterance_id=stable_ulid(
                            "utterance", claim.run.run_id, value.ordinal
                        ),
                        session_id=claim.run.session_id,
                        run_id=claim.run.run_id,
                        source_artifact_id=source.artifact_id,
                        speaker_track_id=speaker_track_id,
                        original_speaker_track_id=speaker_track_id,
                        ordinal=value.ordinal,
                        start_ms=value.start_ms,
                        end_ms=value.end_ms,
                        start_at=captured_start
                        + timedelta(milliseconds=value.start_ms),
                        end_at=captured_start + timedelta(milliseconds=value.end_ms),
                        text=value.text,
                        original_text=value.text,
                        identity=SelfIdentity(value.identity),
                        original_identity=SelfIdentity(value.identity),
                        identity_evidence=dict(value.identity_evidence),
                        evidence=value.evidence,
                        revision=1,
                        status="active",
                        created_at=now,
                        updated_at=now,
                    )
                    if uow.evidence.add_utterance(utterance):
                        utterance = uow.evidence.get_utterance(utterance.utterance_id)
                        uow.changes.append(
                            "utterance",
                            utterance.utterance_id,
                            utterance.revision,
                            ChangeOperation.UPSERT.value,
                            utterance_dto(
                                utterance,
                                speaker_label=uow.evidence.speaker_label(utterance.speaker_track_id),
                                original_speaker_label=value.speaker_label,
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
            snapshot = uow.processing.fail_stage(claim, error, log_summary, retryable)
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

    def cancel_claim(self, claim: ProcessingClaim, reason: str) -> ProcessingSnapshot:
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
