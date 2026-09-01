from __future__ import annotations

import shutil
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork, V3Database
from allday_asr.v3.application import (
    CorrectUtteranceCommand,
    CorrectionInvalidationService,
    KnowledgeArchitectureService,
)
from allday_asr.v3.domain import (
    Artifact,
    AudioAsset,
    AudioFormat,
    AudioReplica,
    AudioReplicaState,
    CaptureSegment,
    Device,
    DeviceKind,
    DeviceStatus,
    GenerationSubmission,
    KnowledgeLayer,
    ProcessingRun,
    ProcessingStatus,
    ProposalKind,
    RecordingSession,
    RecordingSessionState,
    SelfIdentity,
    SpeakerTrack,
    Utterance,
)


TEST_ROOT = Path(__file__).parent
SESSION_ID = "01990d5a-7c00-7000-8000-000000000001"
UTTERANCE_ID = "01990d5a-7c00-7000-8000-000000000002"
SPEAKER_ID = "01990d5a-7c00-7000-8000-000000000003"


class V32ThreeLayerKnowledgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TEST_ROOT / f"v32-knowledge-{uuid4().hex}"
        self.database = V3Database.open(self.root / "core.sqlite3")
        self.now = datetime(2026, 8, 31, 16, 0, tzinfo=timezone.utc)
        self._seed()
        def factory() -> SqliteUnitOfWork:
            return SqliteUnitOfWork(
                self.database, now=lambda: _timestamp(self.now)
            )
        self.knowledge = KnowledgeArchitectureService(factory, now=lambda: self.now)
        self.corrections = CorrectionInvalidationService(
            factory, now=lambda: self.now + timedelta(minutes=1)
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_model_proposals_do_not_mutate_until_accepted_and_history_is_kept(
        self,
    ) -> None:
        evidence = self.knowledge.list_evidence(SESSION_ID)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["asset_start_ms"], 100)
        self.assertEqual(evidence[0]["asset_end_ms"], 900)

        create = _event_submission(
            operation="create",
            expected_revision=0,
            patch={"title": "明天十点见面", "time": "10:00"},
        )
        receipt = self.knowledge.submit_generation(create)
        proposal_id = receipt["proposals"][0]["proposal_id"]

        self.assertEqual(self.knowledge.list_events(SESSION_ID), ())
        self.assertEqual(
            self.knowledge.list_proposals("pending")[0]["proposal_id"],
            proposal_id,
        )

        resolution = self.knowledge.accept_proposal(proposal_id, "reviewer")
        event_id = resolution.resource_id
        self.assertIsNotNone(event_id)
        events = self.knowledge.list_events(SESSION_ID)
        self.assertEqual(events[0]["payload"]["time"], "10:00")
        self.assertEqual(events[0]["evidence"][0]["evidence_id"], UTTERANCE_ID)

        update = _event_submission(
            operation="update",
            expected_revision=1,
            event_id=event_id,
            patch={"time": "11:00"},
        )
        first_rerun = self.knowledge.submit_generation(update)
        second_rerun = self.knowledge.submit_generation(update)
        self.assertEqual(
            second_rerun["generation_number"],
            first_rerun["generation_number"] + 1,
        )
        self.knowledge.accept_proposal(
            first_rerun["proposals"][0]["proposal_id"], "reviewer"
        )

        current = self.knowledge.list_events(SESSION_ID)[0]
        history = self.knowledge.event_history(event_id)
        self.assertEqual(current["revision"], 2)
        self.assertEqual(current["payload"]["time"], "11:00")
        self.assertEqual(
            [operation["operation_kind"] for operation in history],
            ["create", "update"],
        )
        self.assertEqual(history[0]["payload"]["time"], "10:00")
        self.assertEqual(history[1]["payload"]["time"], "11:00")

    def test_utterance_correction_cascades_to_event_memory_and_recompute_queue(
        self,
    ) -> None:
        event_receipt = self.knowledge.submit_generation(_event_submission())
        event_resolution = self.knowledge.accept_proposal(
            event_receipt["proposals"][0]["proposal_id"], "reviewer"
        )
        event_id = event_resolution.resource_id
        assert event_id is not None

        memory_submission = GenerationSubmission(
            layer=KnowledgeLayer.MEMORY,
            producer="memory-fixture",
            producer_version="1.0.0",
            model="fixture-model",
            prompt_version="memory-prompt.1",
            extractor_version="memory-extractor.1",
            input_scope={"session_id": SESSION_ID, "window": "2026-08-31"},
            proposals=(
                (
                    ProposalKind.MEMORY_RECORD,
                    {
                        "session_id": SESSION_ID,
                        "memory_kind": "daily_summary",
                        "subject_type": "day",
                        "subject_id": "2026-08-31",
                        "content": {"summary": "约定明天见面。"},
                        "input_event_ids": [event_id],
                    },
                    (),
                ),
            ),
        )
        memory_receipt = self.knowledge.submit_generation(memory_submission)
        memory_resolution = self.knowledge.accept_proposal(
            memory_receipt["proposals"][0]["proposal_id"], "reviewer"
        )
        memory_id = memory_resolution.resource_id

        affected = self.knowledge.affected_by("utterance", UTTERANCE_ID)
        self.assertIn(
            {"type": "event", "id": event_id, "revision": 1},
            affected["affected"],
        )
        self.assertIn(
            {"type": "memory", "id": memory_id, "revision": 1},
            affected["affected"],
        )

        self.corrections.correct_utterance(
            CorrectUtteranceCommand(
                utterance_id=UTTERANCE_ID,
                expected_revision=1,
                text="我们改成明天十一点见。",
                actor="desktop-user",
            )
        )

        event = self.knowledge.list_events(SESSION_ID)[0]
        memory = self.knowledge.list_memories(session_id=SESSION_ID)[0]
        requests = self.knowledge.list_recompute_requests("queued")
        self.assertEqual(event["derivation_status"], "stale")
        self.assertEqual(memory["derivation_status"], "stale")
        self.assertEqual(
            {(request["target_type"], request["target_id"]) for request in requests},
            {("event", event_id), ("memory", memory_id)},
        )
        self.assertEqual(len(self.knowledge.list_invalidations()), 2)

        event_rerun = self.knowledge.submit_generation(
            _event_submission(
                operation="update",
                expected_revision=1,
                event_id=event_id,
                patch={"title": "明天十一点见面", "time": "11:00"},
            )
        )
        self.knowledge.accept_proposal(
            event_rerun["proposals"][0]["proposal_id"], "reviewer"
        )
        memory_rerun = GenerationSubmission(
            layer=KnowledgeLayer.MEMORY,
            producer="memory-fixture",
            producer_version="1.0.0",
            model="fixture-model",
            prompt_version="memory-prompt.1",
            extractor_version="memory-extractor.1",
            input_scope={"session_id": SESSION_ID, "window": "2026-08-31"},
            proposals=(
                (
                    ProposalKind.MEMORY_RECORD,
                    {
                        "memory_id": memory_id,
                        "session_id": SESSION_ID,
                        "memory_kind": "daily_summary",
                        "subject_type": "day",
                        "subject_id": "2026-08-31",
                        "content": {"summary": "约定改为明天十一点。"},
                        "input_event_ids": [event_id],
                    },
                    (),
                ),
            ),
        )
        memory_rerun_receipt = self.knowledge.submit_generation(memory_rerun)
        self.knowledge.accept_proposal(
            memory_rerun_receipt["proposals"][0]["proposal_id"], "reviewer"
        )

        current_event = self.knowledge.list_events(SESSION_ID)[0]
        memories = self.knowledge.list_memories(session_id=SESSION_ID)
        completed = self.knowledge.list_recompute_requests("succeeded")
        self.assertEqual(current_event["derivation_status"], "active")
        self.assertEqual(current_event["revision"], 2)
        self.assertEqual(memories[0]["version"], 2)
        self.assertEqual(memories[0]["derivation_status"], "active")
        self.assertEqual(len(completed), 2)
        self.assertTrue(all(request["generation_id"] for request in completed))

    def test_rejection_and_immutable_history_are_auditable(self) -> None:
        receipt = self.knowledge.submit_generation(_event_submission())
        proposal_id = receipt["proposals"][0]["proposal_id"]
        rejected = self.knowledge.reject_proposal(
            proposal_id, "reviewer", "not an actual commitment"
        )
        self.assertEqual(rejected.status.value, "rejected")
        self.assertEqual(self.knowledge.list_events(SESSION_ID), ())

        accepted = self.knowledge.submit_generation(_event_submission())
        resolution = self.knowledge.accept_proposal(
            accepted["proposals"][0]["proposal_id"], "reviewer"
        )
        with self.assertRaises(sqlite3.IntegrityError), self.database.transaction() as db:
            db.execute(
                "UPDATE event_operations SET actor = 'tampered' WHERE event_id = ?",
                (resolution.resource_id,),
            )

    def _seed(self) -> None:
        now = self.now
        with SqliteUnitOfWork(self.database) as uow:
            uow.devices.add(
                Device(
                    device_id="computer-1",
                    kind=DeviceKind.COMPUTER,
                    name="Desktop",
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
                    timezone="Asia/Singapore",
                    state=RecordingSessionState.READY_FOR_PROCESSING,
                    revision=1,
                    status_code="ready",
                    current_stage=None,
                    progress=1.0,
                    blocking_reason=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            uow.catalog.add_asset(
                AudioAsset(
                    asset_id="asset-1",
                    sha256="a" * 64,
                    size_bytes=100,
                    duration_ms=1000,
                    format=AudioFormat.WAV,
                    media_id="media-1",
                    created_at=now,
                )
            )
            uow.catalog.add_replica(
                AudioReplica(
                    replica_id="replica-1",
                    asset_id="asset-1",
                    device_id="computer-1",
                    storage_key="sha256/aa/test.wav",
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
            uow.processing_runs.add(
                ProcessingRun(
                    run_id="run-1",
                    session_id=SESSION_ID,
                    pipeline_version="v3.1-test",
                    input_revision=1,
                    status=ProcessingStatus.SUCCEEDED,
                    config_digest="config",
                    current_stage=None,
                    progress=1.0,
                    created_at=now,
                    updated_at=now,
                    completed_at=now,
                )
            )
            uow.artifacts.add(
                Artifact(
                    artifact_id="artifact-1",
                    run_id="run-1",
                    kind="utterance_projection",
                    producer="test",
                    producer_version="1",
                    config_digest="config",
                    input_refs=(),
                    storage_ref="sha256/bb/test.json",
                    status="active",
                    created_at=now,
                )
            )
            uow.evidence.add_speaker_track(
                SpeakerTrack(
                    speaker_track_id=SPEAKER_ID,
                    session_id=SESSION_ID,
                    run_id="run-1",
                    label="SPEAKER_00",
                    source_artifact_id="artifact-1",
                    created_at=now,
                )
            )
            uow.evidence.add_utterance(
                Utterance(
                    utterance_id=UTTERANCE_ID,
                    session_id=SESSION_ID,
                    run_id="run-1",
                    source_artifact_id="artifact-1",
                    speaker_track_id=SPEAKER_ID,
                    original_speaker_track_id=SPEAKER_ID,
                    ordinal=0,
                    start_ms=100,
                    end_ms=900,
                    start_at=now + timedelta(milliseconds=100),
                    end_at=now + timedelta(milliseconds=900),
                    text="我们明天十点见。",
                    original_text="我们明天十点见。",
                    identity=SelfIdentity.UNKNOWN,
                    original_identity=SelfIdentity.UNKNOWN,
                    identity_evidence={"source": "none", "decision": "unknown"},
                    evidence={"asset_id": "asset-1"},
                    revision=1,
                    status="active",
                    created_at=now,
                    updated_at=now,
                )
            )


def _event_submission(
    *,
    operation: str = "create",
    expected_revision: int = 0,
    event_id: str | None = None,
    patch: dict[str, object] | None = None,
) -> GenerationSubmission:
    payload: dict[str, object] = {
        "operation": operation,
        "session_id": SESSION_ID,
        "event_kind": "appointment",
        "expected_revision": expected_revision,
        "patch": patch or {"title": "明天十点见面", "time": "10:00"},
    }
    if event_id is not None:
        payload["event_id"] = event_id
    return GenerationSubmission(
        layer=KnowledgeLayer.EVENT,
        producer="event-fixture",
        producer_version="1.0.0",
        model="fixture-model",
        prompt_version="event-prompt.1",
        extractor_version="event-extractor.1",
        input_scope={"session_id": SESSION_ID, "start_ms": 0, "end_ms": 1000},
        proposals=(
            (
                ProposalKind.EVENT_OPERATION,
                payload,
                (UTTERANCE_ID,),
            ),
        ),
    )


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


if __name__ == "__main__":
    unittest.main()
