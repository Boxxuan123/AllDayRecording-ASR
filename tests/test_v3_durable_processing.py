from __future__ import annotations

import json
import shutil
import time
import unittest
import wave
from io import BytesIO
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from allday_asr.v3.adapters.files import ContentAddressedStore
from allday_asr.v3.adapters.backup import FilesystemSessionBackupAdapter
from allday_asr.v3.adapters.models_v2 import QualityWorkflowV2Adapter
from allday_asr.v3.adapters.models_v2 import V2SessionMaterializer
from allday_asr.v3.adapters.models import (
    NativeAsrSettings,
    NativeDiarizationSettings,
    NativeModelPipelineAdapter,
)
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork, V3Database
from allday_asr.v3.application import (
    AdmissionService,
    CorrectUtteranceCommand,
    CorrectionInvalidationService,
    DurableProcessingService,
    DurableProcessingWorker,
    RecordBackupEvidenceCommand,
    SubmitProcessingCommand,
    UtteranceCorrectionOperationHandler,
)
from allday_asr.v3.domain import (
    AudioAsset,
    AudioFormat,
    AudioReplica,
    AudioReplicaState,
    CaptureSegment,
    ClientOperation,
    Device,
    DeviceKind,
    DeviceStatus,
    RecordingSession,
    RecordingSessionState,
    SessionManifest,
    stable_ulid,
)
from allday_asr.v3.ports.processing import (
    ArtifactDependencyOutput,
    SpeakerProjectionOutput,
    StageArtifactOutput,
    StageExecutionContext,
    StageExecutionControl,
    StageExecutionResult,
    UtteranceProjectionOutput,
)
from allday_asr.storage.database import Database


SESSION_ID = stable_ulid("durable-processing-test", "session")


TEST_ROOT = Path(__file__).parent


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 31, 2, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.value

    def text(self) -> str:
        return self.value.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class _SuccessfulAdapter:
    def execute(
        self, context: StageExecutionContext, control: StageExecutionControl
    ) -> StageExecutionResult:
        stage = context.claim.stage.stage
        control.heartbeat({"stage": stage})
        if stage == "asr_and_alignment":
            return StageExecutionResult(
                checkpoint={"tokens": 2},
                artifacts=(
                    StageArtifactOutput(
                        kind="v2_evidence_snapshot",
                        payload=b'{"fixture":true}',
                        producer="test-v2",
                        producer_version="1",
                    ),
                ),
            )
        if stage == "utterance_projection":
            return StageExecutionResult(
                speakers=(SpeakerProjectionOutput("speaker-1"),),
                utterances=(
                    UtteranceProjectionOutput(
                        ordinal=0,
                        start_ms=100,
                        end_ms=900,
                        text="测试",
                        speaker_label="speaker-1",
                        evidence={"token_ids": [1, 2]},
                    ),
                ),
            )
        if stage == "semantic_evidence_optional":
            utterance_id = stable_ulid("utterance", context.claim.run.run_id, 0)
            return StageExecutionResult(
                artifacts=(
                    StageArtifactOutput(
                        kind="v2_semantic_evidence",
                        payload=b'{"summary":true}',
                        producer="test-v2",
                        producer_version="1",
                        dependencies=(
                            ArtifactDependencyOutput(
                                "utterance", utterance_id, 1
                            ),
                        ),
                    ),
                ),
            )
        return StageExecutionResult(checkpoint={"stage": stage})


class _RepeatedTimelineAdapter(_SuccessfulAdapter):
    def __init__(self, count: int) -> None:
        self.count = count

    def execute(
        self, context: StageExecutionContext, control: StageExecutionControl
    ) -> StageExecutionResult:
        if context.claim.stage.stage != "utterance_projection":
            return super().execute(context, control)
        control.heartbeat({"stage": "utterance_projection"})
        return StageExecutionResult(
            speakers=(SpeakerProjectionOutput("speaker-current"),),
            utterances=tuple(
                UtteranceProjectionOutput(
                    ordinal=index,
                    start_ms=index,
                    end_ms=index + 1,
                    text="真实重复",
                    speaker_label="speaker-current",
                    evidence={"fixture_index": index},
                )
                for index in range(self.count)
            ),
        )


class _FailOnceAdapter(_SuccessfulAdapter):
    def __init__(self) -> None:
        self.failed = False

    def execute(
        self, context: StageExecutionContext, control: StageExecutionControl
    ) -> StageExecutionResult:
        if context.claim.stage.stage == "diarization" and not self.failed:
            self.failed = True
            raise RuntimeError("injected model failure")
        return super().execute(context, control)


class _OptionalFailureAdapter(_SuccessfulAdapter):
    def execute(
        self, context: StageExecutionContext, control: StageExecutionControl
    ) -> StageExecutionResult:
        if context.claim.stage.stage == "semantic_evidence_optional":
            raise RuntimeError("optional semantic fixture failed")
        return super().execute(context, control)


class _LeaseExpiringAdapter(_SuccessfulAdapter):
    def __init__(self, clock: _Clock) -> None:
        self.clock = clock

    def execute(
        self, context: StageExecutionContext, control: StageExecutionControl
    ) -> StageExecutionResult:
        self.clock.advance(11)
        control.heartbeat({"expired_inside_adapter": True})
        return super().execute(context, control)


class _LongRunningAdapter(_SuccessfulAdapter):
    def __init__(self, clock: _Clock) -> None:
        self.clock = clock

    def execute(
        self, context: StageExecutionContext, control: StageExecutionControl
    ) -> StageExecutionResult:
        for _ in range(2):
            self.clock.advance(8)
            time.sleep(0.03)
        return super().execute(context, control)


class _StaticV2Executor:
    def execute(self, session_id: str, progress) -> dict[str, object]:
        progress("fixture", session_id)
        source = {
            "source_object_id": 1,
            "source_sha256": "a" * 64,
            "source_start_ms": 100,
            "source_end_ms": 600,
        }
        return {
            "format": "AllDayRecording V2 evidence snapshot v1",
            "summary": {"asr_run_id": 11, "diarization_run_id": 12},
            "tokens": [
                {
                    "id": 1,
                    "text": "你",
                    "start_ms": 100,
                    "end_ms": 300,
                    "speaker": "SPEAKER_00",
                    "speaker_kind": "primary",
                    "has_overlap": False,
                    "source_refs": [source],
                },
                {
                    "id": 2,
                    "text": "好",
                    "start_ms": 320,
                    "end_ms": 600,
                    "speaker": "SPEAKER_00",
                    "speaker_kind": "primary",
                    "has_overlap": False,
                    "source_refs": [source],
                },
            ],
            "turns": [
                {
                    "id": 1,
                    "label": "SPEAKER_00",
                    "kind": "regular",
                    "start_ms": 100,
                    "end_ms": 600,
                    "source_refs": [source],
                }
            ],
            "utterances": [
                {
                    "key": "conversation-0001:utterance-0001",
                    "start_ms": 100,
                    "end_ms": 600,
                    "speaker": "SPEAKER_00",
                    "text": "你好",
                    "identity": "self",
                    "identity_evidence": {
                        "source": "voiceprint",
                        "decision": "self",
                        "reason": "all_clean_windows_above_self_threshold",
                        "policy_version": "holdout-accepted-1",
                        "calibration_accepted": True,
                    },
                    "evidence": {"token_ids": [1, 2], "source_refs": [source]},
                }
            ],
        }


class _NativeAsrBackend:
    model_id = "fixture-asr"
    backend_name = "fixture-native-asr"
    model_revision = "1"

    def __init__(self, role: str) -> None:
        self.role = role

    def transcribe(self, audio_path: Path, *, language: str | None):
        del audio_path, language
        return SimpleNamespace(
            text="你好",
            language="zh",
            tokens=(
                SimpleNamespace(
                    text="你",
                    start_seconds=0.1,
                    end_seconds=0.3,
                    confidence=0.9,
                    metadata={"speech_gate_committed": True},
                ),
                SimpleNamespace(
                    text="好",
                    start_seconds=0.32,
                    end_seconds=0.6,
                    confidence=0.9,
                    metadata={"speech_gate_committed": True},
                ),
            ),
            raw_response={"fixture": True},
        )

    def parameters(self):
        return {"fixture": True}

    def close(self) -> None:
        return None


class _NativeDiarizationBackend:
    model_id = "fixture-diarization"
    backend_name = "fixture-native-diarization"
    model_revision = "1"

    def ensure_loaded(self) -> None:
        return None

    def diarize(self, audio_path: Path, **kwargs):
        del audio_path, kwargs
        turn = SimpleNamespace(
            start_ms=0,
            end_ms=900,
            speaker_label="SPEAKER_00",
            confidence=0.95,
            metadata={"fixture": True},
        )
        return SimpleNamespace(
            regular_turns=(turn,),
            exclusive_turns=(turn,),
            raw_response={"fixture": True},
        )

    def parameters(self):
        return {"fixture": True}

    def close(self) -> None:
        return None


class V3DurableProcessingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TEST_ROOT / f"v3d-{uuid4().hex}"
        self.database = V3Database.open(self.root / "core.sqlite3")
        self.audio_store = ContentAddressedStore(self.root / "audio")
        self.artifact_store = ContentAddressedStore(self.root / "artifacts")
        self.clock = _Clock()
        self.uow_factory = lambda: SqliteUnitOfWork(
            self.database, now=self.clock.text
        )
        self.service = DurableProcessingService(
            self.uow_factory,
            self.artifact_store,
            now=self.clock.now,
            lease_seconds=10,
        )
        self.admission = AdmissionService(self.uow_factory, now=self.clock.now)
        self._seed_admitted_session()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_submission_is_immediately_durable_and_deduplicated(self) -> None:
        command = self._command()
        first = self.service.submit(command)
        replay = self.service.submit(command)
        self.assertEqual(first.job.job_id, replay.job.job_id)
        self.assertEqual(first.job.status, "queued")
        self.assertEqual(len(first.stages), 9)
        with self.assertRaisesRegex(ValueError, "different config"):
            self.service.submit(
                SubmitProcessingCommand(
                    session_id=SESSION_ID,
                    pipeline_version="v3-v2-adapter.1",
                    input_revision=1,
                    config={"profile": "other"},
                )
            )

    def test_successful_submission_is_still_reused(self) -> None:
        first = self.service.submit(self._command())
        worker = DurableProcessingWorker(
            self.service, _SuccessfulAdapter(), worker_id="worker-idempotent"
        )
        for _ in range(9):
            self.assertTrue(worker.run_once())
        self.assertEqual(self.service.get(first.job.job_id).job.status, "succeeded")

        replay = self.service.submit(self._command())

        self.assertEqual(replay.job.job_id, first.job.job_id)
        self.assertEqual(replay.run.run_id, first.run.run_id)
        with self.database.read() as connection:
            run_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM processing_runs
                    WHERE session_id = ? AND input_revision = ?
                      AND pipeline_version = ?
                    """,
                    (SESSION_ID, 1, "v3-v2-adapter.1"),
                ).fetchone()[0]
            )
        self.assertEqual(run_count, 1)

    def test_changed_config_or_force_can_create_a_new_run_after_success(self) -> None:
        first = self.service.submit(self._command())
        first_worker = DurableProcessingWorker(
            self.service, _SuccessfulAdapter(), worker_id="worker-first-config"
        )
        for _ in range(9):
            self.assertTrue(first_worker.run_once())

        changed = self.service.submit(
            SubmitProcessingCommand(
                session_id=SESSION_ID,
                pipeline_version="v3-v2-adapter.1",
                input_revision=1,
                config={"profile": "changed"},
            )
        )
        self.assertNotEqual(changed.run.run_id, first.run.run_id)
        changed_worker = DurableProcessingWorker(
            self.service, _SuccessfulAdapter(), worker_id="worker-changed-config"
        )
        for _ in range(9):
            self.assertTrue(changed_worker.run_once())

        forced = self.service.submit(
            SubmitProcessingCommand(
                session_id=SESSION_ID,
                pipeline_version="v3-v2-adapter.1",
                input_revision=1,
                config={"profile": "changed"},
                force_reprocess=True,
            )
        )
        self.assertNotEqual(forced.run.run_id, changed.run.run_id)

    def test_desktop_timeline_uses_latest_successful_production_run(self) -> None:
        historical = self.service.submit(self._command())
        historical_worker = DurableProcessingWorker(
            self.service, _SuccessfulAdapter(), worker_id="worker-historical"
        )
        for _ in range(9):
            self.assertTrue(historical_worker.run_once())
        self.assertEqual(
            self.service.get(historical.job.job_id).job.status, "succeeded"
        )

        self.clock.advance(1)
        current = self.service.submit(
            SubmitProcessingCommand(
                session_id=SESSION_ID,
                pipeline_version="v3-current-timeline.1",
                input_revision=1,
                config={"profile": "current"},
            )
        )
        current_worker = DurableProcessingWorker(
            self.service,
            _RepeatedTimelineAdapter(501),
            worker_id="worker-current",
        )
        for _ in range(9):
            self.assertTrue(current_worker.run_once())
        self.assertEqual(self.service.get(current.job.job_id).job.status, "succeeded")

        with self.uow_factory() as uow:
            detail = uow.desktop.session_detail(SESSION_ID)

        self.assertEqual(len(detail["runs"]), 2)
        self.assertEqual(len(detail["utterances"]), 501)
        self.assertEqual(
            {utterance["text"] for utterance in detail["utterances"]},
            {"真实重复"},
        )
        self.assertEqual(
            [utterance["start_ms"] for utterance in detail["utterances"]],
            list(range(501)),
        )
        self.assertEqual(
            [track["label"] for track in detail["speaker_tracks"]],
            ["speaker-current"],
        )

    def test_shadow_submission_is_explicit_and_keeps_backup_blocker(self) -> None:
        shadow_id = stable_ulid("durable-processing-test", "shadow-session")
        now = self.clock.now()
        manifest_payload = {
            "format": "AllDayRecording session manifest v1",
            "sessionKey": "v3-shadow-session",
            "sessionStartedAt": int(now.timestamp() * 1000),
            "device": "Harmony Phone",
            "timezone": "UTC",
            "audio": {"sampleRate": 16000, "channels": 1, "bitsPerSample": 16},
            "chunks": [{"index": 0, "fileName": "segment_0.wav", "firstSample": 0, "sampleCount": 16000}],
            "completedSegments": 1,
            "totalSamples": 16000,
            "continuityValid": True,
        }
        manifest_bytes = json.dumps(manifest_payload, sort_keys=True).encode()
        stored_manifest = self.artifact_store.put_bytes(manifest_bytes)
        with self.uow_factory() as uow:
            uow.catalog.add_session(
                RecordingSession(
                    session_id=shadow_id,
                    captured_start=now,
                    captured_end=now + timedelta(seconds=1),
                    timezone="UTC",
                    state=RecordingSessionState.ADMISSION_PENDING,
                    revision=1,
                    status_code="backup_required",
                    current_stage="backup_admission",
                    progress=0.0,
                    blocking_reason="backup_restore_evidence_required",
                    created_at=now,
                    updated_at=now,
                )
            )
            uow.catalog.add_segment(
                CaptureSegment(
                    segment_id="segment-shadow",
                    session_id=shadow_id,
                    asset_id="asset-1",
                    replica_id="replica-1",
                    sequence=0,
                    session_start_ms=0,
                    session_end_ms=1000,
                    source_start_ms=0,
                    source_end_ms=1000,
                    start_sample=0,
                    captured_at=now,
                )
            )
            uow.catalog.add_manifest(
                SessionManifest(
                    manifest_id="manifest-shadow",
                    session_id=shadow_id,
                    schema_version="1",
                    sha256=stored_manifest.sha256,
                    storage_ref=stored_manifest.storage_key,
                    entries=manifest_payload,
                    created_at=now,
                )
            )
        with self.assertRaisesRegex(ValueError, "backup_restore_evidence_required"):
            self.service.submit(
                SubmitProcessingCommand(
                    session_id=shadow_id,
                    pipeline_version="v3-native.production",
                    input_revision=1,
                    config={},
                )
            )
        shadow = self.service.submit(
            SubmitProcessingCommand(
                session_id=shadow_id,
                pipeline_version="v3-native.shadow",
                input_revision=1,
                config={},
                admission_mode="shadow",
            )
        )
        self.assertEqual(shadow.job.request["admission_mode"], "shadow")
        with self.database.read() as connection:
            state = connection.execute(
                "SELECT state, blocking_reason FROM recording_sessions WHERE session_id = ?",
                (shadow_id,),
            ).fetchone()
        self.assertEqual(tuple(state), ("admission_pending", "backup_restore_evidence_required"))

    def test_worker_persists_artifacts_projection_and_selective_staleness(self) -> None:
        submitted = self.service.submit(self._command())
        worker = DurableProcessingWorker(
            self.service, _SuccessfulAdapter(), worker_id="worker-1"
        )
        for _ in range(9):
            self.assertTrue(worker.run_once())
        self.assertFalse(worker.run_once())
        completed = self.service.get(submitted.job.job_id)
        self.assertEqual(completed.job.status, "succeeded")
        self.assertEqual(completed.run.status.value, "succeeded")
        self.assertEqual(len(completed.attempts), 9)

        with self.database.read() as connection:
            utterance = connection.execute("SELECT * FROM utterances").fetchone()
            artifact_count = int(
                connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
            )
            attempt_count = int(
                connection.execute("SELECT COUNT(*) FROM stage_attempts").fetchone()[0]
            )
            backup_before = connection.execute(
                "SELECT digest, status FROM backup_evidence"
            ).fetchone()
        self.assertIsNotNone(utterance)
        self.assertEqual(artifact_count, 2)
        with self.uow_factory() as uow:
            desktop_utterance = uow.desktop.session_detail(SESSION_ID)["utterances"][0]
        self.assertEqual(
            set(desktop_utterance),
            {
                "utterance_id",
                "session_id",
                "speaker_track_id",
                "speaker_label",
                "original_speaker_track_id",
                "original_speaker_label",
                "identity",
                "original_identity",
                "identity_evidence",
                "start_ms",
                "end_ms",
                "start_at",
                "end_at",
                "text",
                "original_text",
                "revision",
                "status",
                "evidence",
            },
        )
        self.assertEqual(desktop_utterance["speaker_label"], "speaker-1")
        self.assertEqual(desktop_utterance["original_text"], "测试")
        self.assertEqual(desktop_utterance["identity"], "unknown")
        self.assertEqual(
            desktop_utterance["identity_evidence"]["reason"],
            "no_identity_evidence",
        )
        self.assertEqual(
            desktop_utterance["start_at"], "2026-08-31T02:00:00.100000Z"
        )
        corrected = CorrectionInvalidationService(
            self.uow_factory, now=self.clock.now
        ).correct_utterance(
            CorrectUtteranceCommand(
                utterance_id=str(utterance["utterance_id"]),
                expected_revision=1,
                text="修正后的测试",
                actor="user",
            )
        )
        self.assertEqual(corrected["revision"], 2)
        self.assertEqual(set(corrected), set(desktop_utterance))
        self.assertEqual(corrected["speaker_label"], "speaker-1")
        self.assertEqual(corrected["original_text"], "测试")
        with self.uow_factory() as uow:
            receipt = UtteranceCorrectionOperationHandler(
                now=self.clock.now
            ).apply(
                ClientOperation(
                    operation_id=stable_ulid("phone-correction", "one"),
                    kind="utterance.correct",
                    base_revision=2,
                    payload={
                        "utterance_id": str(utterance["utterance_id"]),
                        "text": "修正后的测试",
                        "speaker_track_id": None,
                        "identity": "self",
                    },
                ),
                uow,
            )
        self.assertEqual(receipt.status.value, "applied")
        self.assertEqual(receipt.resource_revision, 3)
        history = CorrectionInvalidationService(
            self.uow_factory, now=self.clock.now
        ).correction_history(str(utterance["utterance_id"]))
        self.assertEqual(history["utterance"]["speaker_label"], None)
        self.assertEqual(history["utterance"]["identity"], "self")
        self.assertEqual(history["utterance"]["original_identity"], "unknown")
        self.assertEqual(len(history["operations"]), 2)
        with self.uow_factory() as uow:
            rejected = UtteranceCorrectionOperationHandler(
                now=self.clock.now
            ).apply(
                ClientOperation(
                    operation_id=stable_ulid("phone-correction", "invalid-track"),
                    kind="utterance.correct",
                    base_revision=3,
                    payload={
                        "utterance_id": str(utterance["utterance_id"]),
                        "text": "不会提交的文本",
                        "speaker_track_id": stable_ulid(
                            "speaker-track", "another-session"
                        ),
                        "identity": "not_self",
                    },
                ),
                uow,
            )
        self.assertEqual(rejected.status.value, "rejected")
        after_rejection = CorrectionInvalidationService(
            self.uow_factory, now=self.clock.now
        ).correction_history(str(utterance["utterance_id"]))
        self.assertEqual(after_rejection["utterance"]["revision"], 3)
        self.assertEqual(len(after_rejection["operations"]), 2)
        with self.database.read() as connection:
            stale = connection.execute(
                "SELECT artifact_id FROM artifact_status_events"
            ).fetchall()
            available_audio = connection.execute(
                "SELECT state FROM audio_replicas WHERE replica_id = 'replica-1'"
            ).fetchone()[0]
            attempts_after = int(
                connection.execute("SELECT COUNT(*) FROM stage_attempts").fetchone()[0]
            )
            backup_after = connection.execute(
                "SELECT digest, status FROM backup_evidence"
            ).fetchone()
        self.assertEqual(len(stale), 2)
        self.assertEqual(available_audio, "available")
        self.assertEqual(attempts_after, attempt_count)
        self.assertEqual(tuple(backup_after), tuple(backup_before))
        self.assertEqual(self.service.get(submitted.job.job_id).job.status, "succeeded")

    def test_failed_stage_retry_adds_attempt_and_preserves_completed_artifact(self) -> None:
        submitted = self.service.submit(self._command())
        adapter = _FailOnceAdapter()
        worker = DurableProcessingWorker(
            self.service, adapter, worker_id="worker-retry"
        )
        for _ in range(6):
            worker.run_once()
            if self.service.get(submitted.job.job_id).job.status == "failed_retryable":
                break
        failed = self.service.get(submitted.job.job_id)
        self.assertEqual(failed.job.status, "failed_retryable")
        self.assertEqual(failed.stages[4].status.value, "succeeded")
        with self.database.read() as connection:
            before = int(connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0])
            backup_before = connection.execute(
                "SELECT digest, status FROM backup_evidence"
            ).fetchone()
        self.assertEqual(before, 1)

        self.service.retry(submitted.job.job_id)
        while worker.run_once():
            if self.service.get(submitted.job.job_id).job.status == "succeeded":
                break
        completed = self.service.get(submitted.job.job_id)
        attempts = [
            attempt
            for attempt in completed.attempts
            if attempt.stage_run_id == completed.stages[5].stage_run_id
        ]
        self.assertEqual([value.status.value for value in attempts], ["failed", "succeeded"])
        self.assertIn("injected model failure", attempts[0].error or "")
        self.assertEqual(attempts[0].config, {})
        self.assertEqual(attempts[0].log_summary, "injected model failure")
        with self.database.read() as connection:
            backup_after = connection.execute(
                "SELECT digest, status FROM backup_evidence"
            ).fetchone()
            artifact_count = int(
                connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
            )
        self.assertEqual(tuple(backup_after), tuple(backup_before))
        self.assertEqual(artifact_count, 2)

    def test_expired_lease_is_explicit_and_resumable(self) -> None:
        submitted = self.service.submit(self._command())
        claim = self.service.claim("worker-crashed", {"device": "cpu"})
        self.assertIsNotNone(claim)
        assert claim is not None
        self.service.heartbeat(claim, {"offset": 17})
        self.clock.advance(11)
        restarted = DurableProcessingService(
            self.uow_factory,
            self.artifact_store,
            now=self.clock.now,
            lease_seconds=10,
        )
        self.assertEqual(restarted.recover_expired(), (submitted.job.job_id,))
        failed = restarted.get(submitted.job.job_id)
        self.assertEqual(failed.job.status, "failed_retryable")
        self.assertEqual(failed.attempts[0].status.value, "lost_lease")
        restarted.retry(submitted.job.job_id)
        resumed = restarted.claim("worker-replacement", {"device": "cpu"})
        self.assertIsNotNone(resumed)
        assert resumed is not None
        self.assertEqual(resumed.attempt.attempt_number, 2)
        self.assertEqual(resumed.resume_checkpoint, {"offset": 17})

    def test_worker_recovers_lease_that_expires_inside_adapter(self) -> None:
        submitted = self.service.submit(self._command())
        worker = DurableProcessingWorker(
            self.service,
            _LeaseExpiringAdapter(self.clock),
            worker_id="worker-slow",
        )
        self.assertTrue(worker.run_once())
        recovered = self.service.get(submitted.job.job_id)
        self.assertEqual(recovered.job.status, "failed_retryable")
        self.assertEqual(recovered.attempts[0].status.value, "lost_lease")

    def test_worker_renews_lease_while_long_stage_is_running(self) -> None:
        submitted = self.service.submit(self._command())
        worker = DurableProcessingWorker(
            self.service,
            _LongRunningAdapter(self.clock),
            worker_id="worker-long-stage",
            heartbeat_interval_seconds=0.01,
        )

        self.assertTrue(worker.run_once())

        snapshot = self.service.get(submitted.job.job_id)
        self.assertEqual(snapshot.attempts[0].status.value, "succeeded")
        self.assertNotEqual(snapshot.job.status, "failed_retryable")

    def test_running_job_cancels_at_checkpoint_and_optional_failure_can_advance(
        self,
    ) -> None:
        cancelled = self.service.submit(self._command())
        claim = self.service.claim("worker-cancel", {})
        self.assertIsNotNone(claim)
        assert claim is not None
        requested = self.service.cancel(cancelled.job.job_id, "user requested")
        self.assertEqual(requested.job.status, "cancel_requested")
        self.assertTrue(self.service.heartbeat(claim, {"safe": True}))
        final = self.service.cancel_claim(claim, "user requested")
        self.assertEqual(final.job.status, "cancelled")
        self.assertEqual(final.attempts[0].status.value, "cancelled")

        second = self.service.submit(
            SubmitProcessingCommand(
                session_id=SESSION_ID,
                pipeline_version="v3-v2-adapter.optional-fixture",
                input_revision=1,
                config={"profile": "optional-failure"},
            )
        )
        worker = DurableProcessingWorker(
            self.service, _OptionalFailureAdapter(), worker_id="worker-optional"
        )
        for _ in range(9):
            self.assertTrue(worker.run_once())
        completed = self.service.get(second.job.job_id)
        self.assertEqual(completed.job.status, "succeeded")
        semantic = next(
            stage
            for stage in completed.stages
            if stage.stage == "semantic_evidence_optional"
        )
        self.assertEqual(semantic.status.value, "failed")
        self.assertIn("optional semantic fixture failed", semantic.error or "")

    def test_v2_adapter_preserves_fixture_counts_and_coordinates(self) -> None:
        submitted = self.service.submit(self._command())
        worker = DurableProcessingWorker(
            self.service,
            QualityWorkflowV2Adapter(_StaticV2Executor()),
            worker_id="worker-v2-fixture",
        )
        for _ in range(9):
            self.assertTrue(worker.run_once())
        completed = self.service.get(submitted.job.job_id)
        self.assertEqual(completed.job.status, "succeeded")
        with self.database.read() as connection:
            snapshot_artifact = connection.execute(
                "SELECT metadata_json, storage_ref FROM artifacts WHERE kind = 'v2_evidence_snapshot'"
            ).fetchone()
            utterance = connection.execute("SELECT * FROM utterances").fetchone()
            turns = json.loads(str(snapshot_artifact["metadata_json"]))["turn_count"]
        with self.artifact_store.open(str(snapshot_artifact["storage_ref"])) as source:
            snapshot = json.load(source)
        metadata = json.loads(str(snapshot_artifact["metadata_json"]))
        self.assertEqual(metadata["token_count"], 2)
        self.assertEqual(turns, 1)
        self.assertEqual(metadata["utterance_count"], 1)
        self.assertEqual((utterance["start_ms"], utterance["end_ms"]), (100, 600))
        self.assertEqual(utterance["text"], "你好")
        self.assertEqual(utterance["identity"], "self")
        self.assertEqual(utterance["original_identity"], "self")
        self.assertEqual(
            json.loads(utterance["identity_evidence_json"])["policy_version"],
            "holdout-accepted-1",
        )
        self.assertEqual(
            snapshot["tokens"][0]["source_refs"][0]["source_start_ms"], 100
        )
        self.assertEqual(
            snapshot["tokens"][0]["source_refs"][0]["source_end_ms"], 600
        )

    def test_native_adapter_writes_only_v3_evidence_and_utterances(self) -> None:
        submitted = self.service.submit(
            SubmitProcessingCommand(
                session_id=SESSION_ID,
                pipeline_version="v3-native.1",
                input_revision=1,
                config={"pipeline": "v3-native.1"},
            )
        )
        adapter = NativeModelPipelineAdapter(
            self.database,
            self.audio_store,
            asr=NativeAsrSettings(window_ms=1_000, context_ms=0),
            diarization=NativeDiarizationSettings(),
            primary_factory=lambda: _NativeAsrBackend("primary"),
            secondary_factory=lambda: _NativeAsrBackend("secondary"),
            diarization_factory=_NativeDiarizationBackend,
        )
        worker = DurableProcessingWorker(
            self.service, adapter, worker_id="worker-v3-native"
        )
        while worker.run_once():
            if self.service.get(submitted.job.job_id).job.status == "succeeded":
                break

        completed = self.service.get(submitted.job.job_id)
        self.assertEqual(completed.job.status, "succeeded")
        with self.database.read() as connection:
            kinds = {
                str(row[0]) for row in connection.execute("SELECT kind FROM artifacts")
            }
            utterance = connection.execute(
                "SELECT text, start_ms, end_ms FROM utterances"
            ).fetchone()
        self.assertEqual(
            kinds,
            {
                "v3_asr_evidence",
                "v3_diarization_evidence",
                "v3_transcript_evidence",
                "v3_semantic_input",
            },
        )
        self.assertEqual(tuple(utterance), ("你好", 100, 600))
        self.assertFalse((self.root / "compat-v2.sqlite3").exists())

    def test_native_adapter_normalizes_one_millisecond_ingest_drift(self) -> None:
        drifted_session_id = stable_ulid("durable-processing-test", "drifted-session")
        now = self.clock.now()
        with self.uow_factory() as uow:
            uow.catalog.add_session(
                RecordingSession(
                    session_id=drifted_session_id,
                    captured_start=now,
                    captured_end=now + timedelta(seconds=1),
                    timezone="UTC",
                    state=RecordingSessionState.READY_FOR_PROCESSING,
                    revision=1,
                    status_code="ready",
                    current_stage="backup_admitted",
                    progress=1.0,
                    blocking_reason=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            uow.catalog.add_segment(
                CaptureSegment(
                    segment_id="segment-drifted",
                    session_id=drifted_session_id,
                    asset_id="asset-1",
                    replica_id="replica-1",
                    sequence=0,
                    session_start_ms=1,
                    session_end_ms=1001,
                    source_start_ms=0,
                    source_end_ms=1000,
                    start_sample=0,
                    captured_at=now,
                )
            )
        adapter = NativeModelPipelineAdapter(
            self.database,
            self.audio_store,
            asr=NativeAsrSettings(),
            diarization=NativeDiarizationSettings(),
            primary_factory=lambda: _NativeAsrBackend("primary"),
            secondary_factory=lambda: _NativeAsrBackend("secondary"),
            diarization_factory=_NativeDiarizationBackend,
        )

        sources, duration_ms = adapter._sources(drifted_session_id)

        self.assertEqual(duration_ms, 1000)
        self.assertEqual(
            [(source.session_start_ms, source.session_end_ms) for source in sources],
            [(0, 1000)],
        )

    def test_backup_restore_drill_and_native_v2_materialization_are_idempotent(
        self,
    ) -> None:
        backup = FilesystemSessionBackupAdapter(
            self.database, self.audio_store, self.artifact_store
        ).backup(
            SESSION_ID,
            self.root / "independent-backup",
            storage_kind="independent_device",
        )
        self.assertEqual(backup.file_count, 2)
        self.assertEqual(len(backup.digest), 64)
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            FilesystemSessionBackupAdapter(
                self.database, self.audio_store, self.artifact_store
            ).backup(
                SESSION_ID,
                self.audio_store.root,
                storage_kind="independent_device",
            )

        v2_database = Database.open(self.root / "compat-v2.sqlite3")
        materializer = V2SessionMaterializer(
            self.database,
            self.audio_store,
            self.artifact_store,
            self.root / "compat-sessions",
        )
        first = materializer.resolve(SESSION_ID, v2_database)
        second = materializer.resolve(SESSION_ID, v2_database)
        self.assertEqual(first, second)
        session = v2_database.get_recording_session(first)
        self.assertEqual(int(session["duration_ms"]), 1000)

    def test_backup_preserves_the_catalog_audio_format_in_the_file_name(
        self,
    ) -> None:
        database = MagicMock()
        connection = database.read.return_value.__enter__.return_value
        connection.execute.side_effect = [
            MagicMock(
                fetchone=MagicMock(
                    return_value={"sha256": "a" * 64, "storage_ref": "manifest"}
                )
            ),
            MagicMock(
                fetchall=MagicMock(
                    return_value=[
                        {
                            "sequence": 0,
                            "sha256": "b" * 64,
                            "format": "m4a",
                            "storage_key": "audio",
                        }
                    ]
                )
            ),
        ]
        audio_store = MagicMock()
        artifact_store = MagicMock()
        audio_store.path_for.return_value = Path("audio-source")
        artifact_store.path_for.return_value = Path("manifest-source")

        entries = FilesystemSessionBackupAdapter(
            database, audio_store, artifact_store
        )._entries("session-1")

        self.assertEqual(entries[1][0], Path("audio") / f"000000-{'b' * 64}.m4a")

    def _command(self) -> SubmitProcessingCommand:
        return SubmitProcessingCommand(
            session_id=SESSION_ID,
            pipeline_version="v3-v2-adapter.1",
            input_revision=1,
            config={"profile": "fixture"},
        )

    def _seed_admitted_session(self) -> None:
        now = self.clock.now()
        audio = self.audio_store.put_bytes(_wav_bytes())
        manifest_payload = {
            "format": "AllDayRecording session manifest v1",
            "sessionKey": "v3-fixture-session",
            "sessionStartedAt": int(now.timestamp() * 1000),
            "device": "Harmony Phone",
            "timezone": "UTC",
            "audio": {"sampleRate": 16000, "channels": 1, "bitsPerSample": 16},
            "chunks": [
                {
                    "index": 0,
                    "fileName": "segment_0.wav",
                    "firstSample": 0,
                    "sampleCount": 16000,
                }
            ],
            "completedSegments": 1,
            "totalSamples": 16000,
            "continuityValid": True,
        }
        manifest_bytes = json.dumps(
            manifest_payload, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        manifest = self.artifact_store.put_bytes(manifest_bytes)
        with self.uow_factory() as uow:
            uow.devices.add(
                Device(
                    device_id="computer-1",
                    kind=DeviceKind.COMPUTER,
                    name="Computer",
                    status=DeviceStatus.ACTIVE,
                    revision=1,
                    last_seen_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            uow.catalog.add_session(
                RecordingSession(
                    session_id=SESSION_ID,
                    captured_start=now,
                    captured_end=now + timedelta(seconds=1),
                    timezone="UTC",
                    state=RecordingSessionState.COMPUTER_INGESTED,
                    revision=1,
                    status_code="backup_required",
                    current_stage="ingest",
                    progress=1.0,
                    blocking_reason="backup_required",
                    created_at=now,
                    updated_at=now,
                )
            )
            uow.catalog.add_asset(
                AudioAsset(
                    asset_id="asset-1",
                    sha256=audio.sha256,
                    size_bytes=audio.size_bytes,
                    duration_ms=1000,
                    format=AudioFormat.WAV,
                    media_id=audio.media_id,
                    created_at=now,
                )
            )
            uow.catalog.add_replica(
                AudioReplica(
                    replica_id="replica-1",
                    asset_id="asset-1",
                    device_id="computer-1",
                    storage_key=audio.storage_key,
                    state=AudioReplicaState.AVAILABLE,
                    verified_at=now,
                    created_at=now,
                )
            )
            uow.catalog.add_segment(
                CaptureSegment(
                    segment_id="segment-1",
                    session_id=SESSION_ID,
                    asset_id="asset-1",
                    replica_id="replica-1",
                    sequence=0,
                    session_start_ms=0,
                    session_end_ms=1000,
                    source_start_ms=0,
                    source_end_ms=1000,
                    start_sample=0,
                    captured_at=now,
                )
            )
            uow.catalog.add_manifest(
                SessionManifest(
                    manifest_id="manifest-1",
                    session_id=SESSION_ID,
                    schema_version="1",
                    sha256=manifest.sha256,
                    storage_ref=manifest.storage_key,
                    entries=manifest_payload,
                    created_at=now,
                )
            )
        admitted = self.admission.record_verified_backup(
            RecordBackupEvidenceCommand(
                session_id=SESSION_ID,
                provider="fixture-backup",
                storage_kind="independent_device",
                digest="c" * 64,
                restore_checked_at=now,
                metadata={"restore_drill": True},
            )
        )
        self.assertTrue(admitted)


def _wav_bytes() -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b"\x00\x00" * 16000)
    return output.getvalue()


if __name__ == "__main__":
    unittest.main()
