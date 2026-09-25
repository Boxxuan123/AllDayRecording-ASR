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
    IntelligentReminderService,
    KnowledgeArchitectureService,
    ReminderExtractionService,
)
from allday_asr.v3.domain import (
    Artifact,
    AudioAsset,
    AudioFormat,
    AudioReplica,
    AudioReplicaState,
    CaptureSegment,
    CommitmentDirection,
    Device,
    DeviceKind,
    DeviceStatus,
    ProcessingRun,
    ProcessingStatus,
    RecordingSession,
    RecordingSessionState,
    ReminderGenerationSubmission,
    ReminderIntent,
    ReminderOperation,
    SelfIdentity,
    SpeakerTrack,
    Utterance,
)
from allday_asr.v3.ports.reminder_generation import (
    ReminderModelRequest,
    ReminderModelResult,
    ReminderReasoningEffort,
)


TEST_ROOT = Path(__file__).parent
SESSION_ID = "01990d5a-7c00-7000-8000-000000000101"
UTTERANCE_ID = "01990d5a-7c00-7000-8000-000000000102"
SPEAKER_ID = "01990d5a-7c00-7000-8000-000000000103"


class V33IntelligentReminderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TEST_ROOT / f"v33-reminders-{uuid4().hex}"
        self.database = V3Database.open(self.root / "core.sqlite3")
        self.current_time = datetime(2026, 8, 31, 16, 0, tzinfo=timezone.utc)
        self._seed()

        def factory() -> SqliteUnitOfWork:
            return SqliteUnitOfWork(
                self.database, now=lambda: _timestamp(self.current_time)
            )

        self.factory = factory
        self.knowledge = KnowledgeArchitectureService(
            factory, now=lambda: self.current_time
        )
        self.reminders = IntelligentReminderService(
            factory, self.knowledge, now=lambda: self.current_time
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_candidate_requires_confirmation_and_preserves_playable_evidence(
        self,
    ) -> None:
        receipt = self.reminders.submit_generation(_submission(_intent()))
        candidate = receipt["candidates"][0]
        self.assertEqual(candidate["status"], "pending_confirmation")
        self.assertEqual(candidate["proposal_status"], "pending")
        self.assertEqual(candidate["evidence"][0]["media_id"], "media-1")
        self.assertEqual(self.knowledge.list_events(SESSION_ID), ())

        result = self.reminders.confirm(candidate["candidate_id"], "desktop-user")

        self.assertEqual(result["candidate"]["status"], "confirmed")
        self.assertEqual(result["reminder"]["status"], "scheduled")
        self.assertEqual(result["reminder"]["title"], "把文档发给张同学")
        self.assertEqual(result["reminder"]["actor_person_id"], "self")
        self.assertEqual(
            result["reminder"]["commitment_direction"], "self_to_other"
        )
        self.assertEqual(
            self.reminders.feedback(candidate["candidate_id"])[0]["action"],
            "confirm",
        )

    def test_codex_extraction_uses_minimal_transcript_and_keeps_review_gate(
        self,
    ) -> None:
        generator = _FakeReminderGenerator((_codex_create_intent(),))
        extraction = ReminderExtractionService(
            self.factory,
            self.reminders,
            generator,
            allow_auto_apply=False,
            now=lambda: self.current_time,
        )

        result = extraction.extract(SESSION_ID, reasoning_effort="auto")

        self.assertEqual(result["codex"]["reasoning_effort"], "low")
        self.assertEqual(result["candidates"][0]["status"], "pending_confirmation")
        self.assertEqual(generator.requests[0].session_id, SESSION_ID)
        self.assertEqual(
            set(generator.requests[0].utterances[0]),
            {
                "utterance_id",
                "start_at",
                "end_at",
                "speaker_label",
                "speaker_reference_id",
                "identity",
                "text",
                "revision",
            },
        )
        self.assertNotIn("evidence", generator.requests[0].utterances[0])

    def test_codex_empty_result_still_records_successful_generation(self) -> None:
        generator = _FakeReminderGenerator(())
        extraction = ReminderExtractionService(
            self.factory,
            self.reminders,
            generator,
            now=lambda: self.current_time,
        )

        result = extraction.extract(SESSION_ID, reasoning_effort="medium")

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["codex"]["reasoning_effort"], "medium")

    def test_duplicate_active_reminder_is_rejected_without_second_event(self) -> None:
        first = self.reminders.submit_generation(_submission(_intent()))
        self.reminders.confirm(first["candidates"][0]["candidate_id"], "reviewer")

        second = self.reminders.submit_generation(_submission(_intent()))
        duplicate = second["candidates"][0]

        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(duplicate["proposal_status"], "rejected")
        self.assertEqual(len(self.reminders.list_schedules()), 1)
        self.assertEqual(len(self.knowledge.list_events(SESSION_ID)), 1)

    def test_auto_apply_is_fail_closed_and_requires_self_evidence(self) -> None:
        applied = self.reminders.submit_generation(
            _submission(
                _intent(
                    scheduled_at=self.current_time + timedelta(hours=10),
                    confidence=0.99,
                    needs_confirmation=False,
                )
            )
        )["candidates"][0]
        self.assertEqual(applied["status"], "auto_applied")

        low_confidence = self.reminders.submit_generation(
            _submission(
                _intent(
                    title="准备会议材料",
                    scheduled_at=self.current_time + timedelta(hours=11),
                    confidence=0.97,
                    needs_confirmation=False,
                )
            )
        )["candidates"][0]
        self.assertEqual(low_confidence["status"], "pending_confirmation")

        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE utterances SET identity = 'unknown' WHERE utterance_id = ?",
                (UTTERANCE_ID,),
            )
        unknown_identity = self.reminders.submit_generation(
            _submission(
                _intent(
                    title="准备第二次会议",
                    scheduled_at=self.current_time + timedelta(hours=12),
                    confidence=1.0,
                    needs_confirmation=False,
                )
            )
        )["candidates"][0]
        self.assertEqual(unknown_identity["status"], "pending_confirmation")

    def test_update_cancel_and_complete_operate_on_the_same_event(self) -> None:
        created = self._confirmed(_intent())
        event_id = created["reminder"]["event_id"]

        update = self.reminders.submit_generation(
            _submission(
                _intent(
                    operation=ReminderOperation.UPDATE_EVENT,
                    target_event_id=event_id,
                    expected_revision=1,
                    title="把最终文档发给张同学",
                    scheduled_at=self.current_time + timedelta(hours=12),
                )
            )
        )["candidates"][0]
        updated = self.reminders.confirm(update["candidate_id"], "reviewer")
        self.assertEqual(updated["reminder"]["event_revision"], 2)
        self.assertEqual(updated["reminder"]["title"], "把最终文档发给张同学")

        cancel = self.reminders.submit_generation(
            _submission(
                _intent(
                    operation=ReminderOperation.CANCEL_EVENT,
                    target_event_id=event_id,
                    expected_revision=2,
                    title=None,
                    scheduled_at=None,
                    reason="明天不用发了",
                )
            )
        )["candidates"][0]
        cancelled = self.reminders.confirm(cancel["candidate_id"], "reviewer")
        self.assertEqual(cancelled["reminder"]["status"], "cancelled")
        self.assertEqual(
            [item["operation_kind"] for item in self.knowledge.event_history(event_id)],
            ["create", "update", "cancel"],
        )

        another = self._confirmed(
            _intent(
                title="提交周报",
                scheduled_at=self.current_time + timedelta(hours=15),
            )
        )
        another_id = another["reminder"]["event_id"]
        done = self.reminders.submit_generation(
            _submission(
                _intent(
                    operation=ReminderOperation.MARK_DONE,
                    target_event_id=another_id,
                    expected_revision=1,
                    title=None,
                    scheduled_at=None,
                    reason="我已经发了",
                )
            )
        )["candidates"][0]
        completed = self.reminders.confirm(done["candidate_id"], "reviewer")
        self.assertEqual(completed["reminder"]["status"], "completed")

    def test_user_edit_supersedes_candidate_and_applies_replacement(self) -> None:
        original = self.reminders.submit_generation(_submission(_intent()))[
            "candidates"
        ][0]
        changed_time = self.current_time + timedelta(hours=14)

        result = self.reminders.modify(
            original["candidate_id"],
            "desktop-user",
            {
                "title": "发送修改后的文档",
                "scheduled_at": _timestamp(changed_time),
                "location": "办公室",
            },
        )

        self.assertEqual(result["candidate"]["status"], "modified")
        self.assertEqual(result["replacement"]["status"], "confirmed")
        self.assertEqual(result["reminder"]["title"], "发送修改后的文档")
        self.assertEqual(result["reminder"]["scheduled_at"], _timestamp(changed_time))
        self.assertEqual(len(self.knowledge.list_events(SESSION_ID)), 1)

    def test_revision_race_becomes_conflict_instead_of_duplicate_event(self) -> None:
        event_id = self._confirmed(_intent())["reminder"]["event_id"]
        first = self.reminders.submit_generation(
            _submission(
                _intent(
                    operation=ReminderOperation.UPDATE_EVENT,
                    target_event_id=event_id,
                    expected_revision=1,
                    title="十一点发送",
                    scheduled_at=self.current_time + timedelta(hours=11),
                )
            )
        )["candidates"][0]
        second = self.reminders.submit_generation(
            _submission(
                _intent(
                    operation=ReminderOperation.UPDATE_EVENT,
                    target_event_id=event_id,
                    expected_revision=1,
                    title="十二点发送",
                    scheduled_at=self.current_time + timedelta(hours=12),
                )
            )
        )["candidates"][0]
        self.reminders.confirm(second["candidate_id"], "reviewer")

        conflict = self.reminders.confirm(first["candidate_id"], "reviewer")

        self.assertEqual(conflict["status"], "conflict")
        self.assertEqual(conflict["proposal_status"], "rejected")
        self.assertEqual(len(self.knowledge.list_events(SESSION_ID)), 1)
        self.assertEqual(self.knowledge.list_events(SESSION_ID)[0]["revision"], 2)

    def test_due_delivery_and_immutable_feedback(self) -> None:
        result = self._confirmed(
            _intent(scheduled_at=self.current_time + timedelta(hours=1))
        )
        event_id = result["reminder"]["event_id"]
        self.current_time += timedelta(hours=2)
        self.assertEqual(self.reminders.due()[0]["event_id"], event_id)

        delivered = self.reminders.deliver(event_id)
        self.assertEqual(delivered["status"], "delivered")
        candidate_id = result["candidate"]["candidate_id"]
        self.assertEqual(
            [item["action"] for item in self.reminders.feedback(candidate_id)],
            ["confirm", "deliver"],
        )
        with self.assertRaises(sqlite3.IntegrityError), self.database.transaction() as db:
            db.execute(
                "UPDATE reminder_feedback SET actor = 'tampered' WHERE candidate_id = ?",
                (candidate_id,),
            )

    def test_phone_projection_preserves_confirmed_task_and_reviews_source_change(self) -> None:
        result = self._confirmed(_intent())
        event_id = result["reminder"]["event_id"]
        with self.factory() as uow:
            change = next(
                item
                for item in uow.changes.list_after(0, 100)
                if item.resource_type == "reminder"
            )
        self.assertEqual(change.operation.value, "upsert")
        self.assertEqual(change.payload["event_id"], event_id)
        self.assertEqual(change.payload["status"], "scheduled")

        CorrectionInvalidationService(
            self.factory, now=lambda: self.current_time
        ).correct_utterance(
            CorrectUtteranceCommand(
                utterance_id=UTTERANCE_ID,
                expected_revision=1,
                text="我不需要发送文档。",
                actor="desktop-user",
            )
        )

        self.assertEqual(self.reminders.schedule(event_id)["status"], "scheduled")
        self.assertTrue(self.reminders.schedule(event_id)["source_review_required"])
        self.assertEqual(len(self.reminders.due(self.current_time + timedelta(days=2))), 1)
        with self.factory() as uow:
            reminder_changes = [
                item
                for item in uow.changes.list_after(0, 100)
                if item.resource_type == "reminder"
            ]
        self.assertEqual(
            [item.operation.value for item in reminder_changes],
            ["upsert"],
        )
        self.assertEqual(reminder_changes[-1].payload["status"], "scheduled")

    def _confirmed(self, intent: ReminderIntent) -> dict[str, object]:
        candidate = self.reminders.submit_generation(_submission(intent))["candidates"][0]
        return self.reminders.confirm(candidate["candidate_id"], "reviewer")

    def _seed(self) -> None:
        now = self.current_time
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
                    pipeline_version="v3.3-test",
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
                    text="我明天把文档发给张同学。",
                    original_text="我明天把文档发给张同学。",
                    identity=SelfIdentity.SELF,
                    original_identity=SelfIdentity.SELF,
                    identity_evidence={"source": "test", "decision": "self"},
                    evidence={"asset_id": "asset-1"},
                    revision=1,
                    status="active",
                    created_at=now,
                    updated_at=now,
                )
            )


def _intent(
    *,
    operation: ReminderOperation = ReminderOperation.CREATE_TASK,
    title: str | None = "把文档发给张同学",
    scheduled_at: datetime | None = datetime(
        2026, 9, 1, 2, 0, tzinfo=timezone.utc
    ),
    confidence: float = 0.91,
    needs_confirmation: bool = True,
    target_event_id: str | None = None,
    expected_revision: int = 0,
    reason: str | None = None,
) -> ReminderIntent:
    return ReminderIntent(
        operation=operation,
        session_id=SESSION_ID,
        title=title,
        actor_person_id="self",
        commitment_direction=CommitmentDirection.SELF_TO_OTHER,
        related_person_ids=("person-zhang",),
        scheduled_at=scheduled_at,
        location="学校",
        confidence=confidence,
        evidence_utterance_ids=(UTTERANCE_ID,),
        needs_confirmation=needs_confirmation,
        target_event_id=target_event_id,
        expected_revision=expected_revision,
        reason=reason,
    )


def _submission(intent: ReminderIntent) -> ReminderGenerationSubmission:
    return ReminderGenerationSubmission(
        producer="reminder-fixture",
        producer_version="1.0.0",
        model="fixture-model",
        prompt_version="reminder-prompt.1",
        extractor_version="reminder-extractor.1",
        input_scope={"session_id": SESSION_ID, "start_ms": 0, "end_ms": 1000},
        intents=(intent,),
    )


class _FakeReminderGenerator:
    model_label = "codex-test"
    producer_version = "0.147.0"
    prompt_version = "prompt-test"
    extractor_version = "extractor-test"

    def __init__(self, intents: tuple[dict[str, object], ...]) -> None:
        self.intents = intents
        self.requests: list[ReminderModelRequest] = []

    def generate(
        self,
        request: ReminderModelRequest,
        effort: ReminderReasoningEffort,
    ) -> ReminderModelResult:
        self.requests.append(request)
        return ReminderModelResult(
            intents=self.intents,
            turn_id="codex-turn-test",
            reasoning_effort=effort,
            usage={"total_tokens": 123},
        )

    def close(self) -> None:
        pass


def _codex_create_intent() -> dict[str, object]:
    return {
        "operation": "CREATE_TASK",
        "title": "把文档发给张同学",
        "actor_person_id": "self",
        "commitment_direction": "self_to_other",
        "related_person_ids": ["person-zhang"],
        "scheduled_at": "2026-09-01T02:00:00Z",
        "location": "学校",
        "confidence": 0.99,
        "evidence_utterance_ids": [UTTERANCE_ID],
        "needs_confirmation": False,
        "target_event_id": None,
        "expected_revision": 0,
        "reason": "明确的本人承诺和时间",
    }


def _timestamp(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


if __name__ == "__main__":
    unittest.main()
