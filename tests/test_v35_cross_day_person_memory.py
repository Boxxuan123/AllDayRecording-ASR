from __future__ import annotations

import shutil
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork, V3Database
from allday_asr.v3.application import (
    KnowledgeArchitectureService,
    IntelligentReminderService,
    PersonMemoryService,
    SpeakerIdentityService,
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
    PersonMemoryDraft,
    PersonMemoryKind,
    ProcessingRun,
    ProcessingStatus,
    ProposalKind,
    RecordingSession,
    RecordingSessionState,
    ReminderGenerationSubmission,
    ReminderIntent,
    ReminderOperation,
    CommitmentDirection,
    SelfIdentity,
    SpeakerTrack,
    Utterance,
    new_ulid,
)


TEST_ROOT = Path(__file__).parent
NOW = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)


class _UnusedEmbeddingProvider:
    model = "unused"
    model_version = "1"

    def embed(self, tracks):
        return ()


class V35CrossDayPersonMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TEST_ROOT / f"v35-person-memory-{uuid4().hex}"
        self.database = V3Database.open(self.root / "core.sqlite3")
        self.now = NOW
        self.session_ids = {1: new_ulid(), 2: new_ulid()}

        def factory() -> SqliteUnitOfWork:
            return SqliteUnitOfWork(
                self.database, now=lambda: self.now.isoformat()
            )

        self.factory = factory
        self.knowledge = KnowledgeArchitectureService(factory, now=lambda: self.now)
        self.memories = PersonMemoryService(factory, now=lambda: self.now)
        self.reminders = IntelligentReminderService(
            factory, self.knowledge, now=lambda: self.now
        )
        self.people = SpeakerIdentityService(
            factory,
            _UnusedEmbeddingProvider(),
            self.knowledge,
            now=lambda: self.now,
        )
        self._seed_session(1, self.now - timedelta(days=1), "昨天确认合同时间")
        self._seed_session(2, self.now, "今天答应发送修改稿")
        with self.factory() as uow:
            uow.people.create_person("person-a", "张同学", "known", self.now.isoformat())
            uow.people.create_person("person-b", "李同学", "known", self.now.isoformat())
        self._link_person("person-a")
        self.knowledge.materialize_evidence(self.session_ids[1])
        self.knowledge.materialize_evidence(self.session_ids[2])

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_projects_cross_day_fact_and_commitment_with_playable_evidence(self) -> None:
        self._event(
            1,
            "person_fact",
            {
                "title": "张同学是合同项目成员",
                "actor_person_id": "person-a",
                "related_person_ids": ["person-a"],
                "topics": ["合同", "项目"],
            },
        )
        self._event(
            2,
            "commitment",
            {
                "title": "张同学明天带合同原件",
                "actor_person_id": "person-a",
                "related_person_ids": ["person-a"],
                "commitment_direction": "other_to_self",
                "topics": ["合同"],
            },
        )

        refreshed = self.memories.refresh("person-a")
        detail = self.memories.person("person-a")

        self.assertEqual(refreshed["created_count"], 2)
        self.assertEqual(detail["memory_count"], 2)
        self.assertEqual(detail["interaction_count"], 4)
        self.assertEqual(
            datetime.fromisoformat(detail["first_seen_at"].replace("Z", "+00:00")),
            self.now - timedelta(days=1),
        )
        self.assertEqual(
            datetime.fromisoformat(detail["last_seen_at"].replace("Z", "+00:00")),
            self.now,
        )
        self.assertEqual(detail["topics"][0], {"label": "合同", "count": 2})
        commitment = detail["commitments"][0]
        quote = next(item for item in commitment["evidence"] if item["utterance_id"])
        self.assertEqual(quote["text"], "今天答应发送修改稿")
        self.assertEqual(quote["media_id"], "media-2")
        self.assertEqual(commitment["confirmation_status"], "confirmed")

    def test_manual_memory_revise_retract_and_undo_keep_immutable_history(self) -> None:
        memory = self.memories.create(
            PersonMemoryDraft(
                person_id="person-a",
                kind=PersonMemoryKind.SHORT_TERM_STATE,
                summary="这周在上海",
                valid_from=self.now,
                valid_until=self.now + timedelta(days=7),
                evidence_utterance_ids=("utterance-2",),
            )
        )
        revised = self.memories.revise(
            memory["memory_id"],
            summary="这周在苏州",
            details={"topics": ["出差"]},
            confidence=0.95,
            valid_from=self.now,
            valid_until=self.now + timedelta(days=7),
            reminder_event_id=None,
        )
        self.assertEqual(revised["revision"], 2)
        self.assertEqual(revised["summary"], "这周在苏州")
        self.assertEqual(self.memories.retract(memory["memory_id"])["status"], "retracted")
        restored = self.memories.undo(memory["memory_id"])
        self.assertEqual(restored["status"], "active")
        self.assertEqual(restored["summary"], "这周在苏州")

        with self.database.transaction() as connection, self.assertRaisesRegex(
            sqlite3.IntegrityError, "immutable"
        ):
            connection.execute(
                "UPDATE person_memory_entries SET summary = 'tampered' WHERE memory_id = ?",
                (memory["memory_id"],),
            )

    def test_short_term_memory_cannot_silently_become_permanent(self) -> None:
        with self.assertRaisesRegex(ValueError, "require valid_until"):
            PersonMemoryDraft(
                person_id="person-a",
                kind=PersonMemoryKind.SHORT_TERM_STATE,
                summary="正在上海",
                valid_from=self.now,
                evidence_utterance_ids=("utterance-2",),
            )

    def test_commitment_memory_links_to_accepted_reminder_lifecycle(self) -> None:
        generated = self.reminders.submit_generation(
            ReminderGenerationSubmission(
                producer="v35-reminder-fixture",
                producer_version="1",
                model="none",
                prompt_version="1",
                extractor_version="1",
                input_scope={"session_id": self.session_ids[2]},
                intents=(
                    ReminderIntent(
                        operation=ReminderOperation.CREATE_TASK,
                        session_id=self.session_ids[2],
                        title="明天发送合同修改稿",
                        actor_person_id="self",
                        commitment_direction=CommitmentDirection.SELF_TO_OTHER,
                        related_person_ids=("person-a",),
                        scheduled_at=self.now + timedelta(days=1),
                        location=None,
                        confidence=0.95,
                        evidence_utterance_ids=("utterance-2",),
                        needs_confirmation=True,
                    ),
                ),
            )
        )
        candidate_id = generated["candidates"][0]["candidate_id"]
        accepted = self.reminders.confirm(candidate_id, "desktop-user")
        self.assertIn("reminder", accepted, accepted)

        self.memories.refresh("person-a")
        detail = self.memories.person("person-a")
        self.assertTrue(detail["commitments"], detail)
        commitment = detail["commitments"][0]

        self.assertEqual(commitment["reminder_event_id"], accepted["reminder"]["event_id"])
        self.assertEqual(commitment["reminder"]["status"], "scheduled")
        self.assertEqual(
            commitment["details"]["scheduled_time"],
            accepted["reminder"]["scheduled_at"],
        )
        self.assertEqual(
            commitment["details"]["commitment_direction"], "self_to_other"
        )

    def test_profile_aliases_relationships_and_identity_correction_migrate_memory(self) -> None:
        profile = self.memories.update_profile(
            "person-a",
            display_name="张老师",
            aliases=("老张", "张同学", "老张"),
            relationship_labels=("项目成员",),
            notes="合同项目",
        )
        self.assertEqual(profile["aliases"], ["老张", "张同学"])
        self._event(
            2,
            "person_fact",
            {
                "title": "负责合同交付",
                "actor_person_id": "person-a",
                "related_person_ids": ["person-a"],
            },
        )
        self.memories.refresh("person-a")

        result = self.people.label_cluster("cluster-1", "person-b")

        self.assertEqual(result["migrated_memory_count"], 1)
        self.assertEqual(self.memories.person("person-a")["memory_count"], 0)
        moved = self.memories.person("person-b")["memories"][0]
        self.assertEqual(moved["status"], "active")
        self.assertEqual(
            moved["details"]["identity_reconciliation"]["from_person_id"],
            "person-a",
        )

    def _seed_session(self, number: int, captured: datetime, text: str) -> None:
        session_id = self.session_ids[number]
        with self.factory() as uow:
            if number == 1:
                uow.devices.add(
                    Device(
                        device_id="computer-1",
                        kind=DeviceKind.COMPUTER,
                        name="Desktop",
                        status=DeviceStatus.ACTIVE,
                        revision=1,
                        last_seen_at=captured,
                        created_at=captured,
                        updated_at=captured,
                    )
                )
            uow.catalog.add_session(
                RecordingSession(
                    session_id=session_id,
                    captured_start=captured,
                    captured_end=captured + timedelta(seconds=1),
                    timezone="Asia/Singapore",
                    state=RecordingSessionState.READY_FOR_PROCESSING,
                    revision=1,
                    status_code="ready",
                    current_stage=None,
                    progress=1.0,
                    blocking_reason=None,
                    created_at=captured,
                    updated_at=captured,
                )
            )
            uow.catalog.add_asset(
                AudioAsset(
                    asset_id=f"asset-{number}",
                    sha256=f"{number:064x}",
                    size_bytes=100,
                    duration_ms=1000,
                    format=AudioFormat.WAV,
                    media_id=f"media-{number}",
                    created_at=captured,
                )
            )
            uow.catalog.add_replica(
                AudioReplica(
                    replica_id=f"replica-{number}",
                    asset_id=f"asset-{number}",
                    device_id="computer-1",
                    storage_key=f"fixture/{number}.wav",
                    state=AudioReplicaState.AVAILABLE,
                    verified_at=captured,
                    created_at=captured,
                )
            )
            uow.catalog.add_segment(
                CaptureSegment(
                    segment_id=f"segment-{number}",
                    session_id=session_id,
                    asset_id=f"asset-{number}",
                    replica_id=f"replica-{number}",
                    sequence=0,
                    session_start_ms=0,
                    session_end_ms=1000,
                    source_start_ms=0,
                    source_end_ms=1000,
                    start_sample=0,
                    captured_at=captured,
                )
            )
            uow.processing_runs.add(
                ProcessingRun(
                    run_id=f"run-{number}",
                    session_id=session_id,
                    pipeline_version="v3.5-test",
                    input_revision=1,
                    status=ProcessingStatus.SUCCEEDED,
                    config_digest="config",
                    current_stage=None,
                    progress=1.0,
                    created_at=captured,
                    updated_at=captured,
                    completed_at=captured,
                )
            )
            uow.artifacts.add(
                Artifact(
                    artifact_id=f"artifact-{number}",
                    run_id=f"run-{number}",
                    kind="utterance_projection",
                    producer="test",
                    producer_version="1",
                    config_digest="config",
                    input_refs=(),
                    storage_ref=f"fixture/{number}.json",
                    status="active",
                    created_at=captured,
                )
            )
            uow.evidence.add_speaker_track(
                SpeakerTrack(
                    speaker_track_id=f"track-{number}",
                    session_id=session_id,
                    run_id=f"run-{number}",
                    label="SPEAKER_00",
                    source_artifact_id=f"artifact-{number}",
                    created_at=captured,
                )
            )
            uow.evidence.add_utterance(
                Utterance(
                    utterance_id=f"utterance-{number}",
                    session_id=session_id,
                    run_id=f"run-{number}",
                    source_artifact_id=f"artifact-{number}",
                    speaker_track_id=f"track-{number}",
                    original_speaker_track_id=f"track-{number}",
                    ordinal=0,
                    start_ms=100,
                    end_ms=900,
                    start_at=captured + timedelta(milliseconds=100),
                    end_at=captured + timedelta(milliseconds=900),
                    text=text,
                    original_text=text,
                    identity=SelfIdentity.UNKNOWN,
                    original_identity=SelfIdentity.UNKNOWN,
                    identity_evidence={"source": "none", "decision": "unknown"},
                    evidence={"asset_id": f"asset-{number}"},
                    revision=1,
                    status="active",
                    created_at=captured,
                    updated_at=captured,
                )
            )

    def _link_person(self, person_id: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO speaker_clusters (
                  cluster_id, display_label, status, revision, created_at, updated_at
                ) VALUES ('cluster-1', '张同学声纹', 'active', 1, ?, ?)
                """,
                (self.now.isoformat(), self.now.isoformat()),
            )
            for number in (1, 2):
                connection.execute(
                    """
                    INSERT INTO speaker_cluster_memberships (
                      membership_id, cluster_id, speaker_track_id, state, source,
                      confidence, revision, operation_id, created_at, updated_at
                    ) VALUES (?, 'cluster-1', ?, 'active', 'human', 1, 1,
                      'seed-operation', ?, ?)
                    """,
                    (
                        f"membership-{number}",
                        f"track-{number}",
                        self.now.isoformat(),
                        self.now.isoformat(),
                    ),
                )
            connection.execute(
                """
                INSERT INTO person_cluster_links (
                  link_id, cluster_id, person_id, status, confidence, source,
                  revision, operation_id, created_at, updated_at
                ) VALUES ('link-1', 'cluster-1', ?, 'active', 1, 'human', 1,
                  'seed-operation', ?, ?)
                """,
                (person_id, self.now.isoformat(), self.now.isoformat()),
            )

    def _event(self, number: int, kind: str, patch: dict[str, object]) -> str:
        receipt = self.knowledge.submit_generation(
            GenerationSubmission(
                layer=KnowledgeLayer.EVENT,
                producer="v35-fixture",
                producer_version="1",
                model="none",
                prompt_version="1",
                extractor_version="1",
                input_scope={"session_id": self.session_ids[number], "kind": kind},
                proposals=(
                    (
                        ProposalKind.EVENT_OPERATION,
                        {
                            "operation": "create",
                            "session_id": self.session_ids[number],
                            "event_kind": kind,
                            "expected_revision": 0,
                            "patch": patch,
                        },
                        (f"utterance-{number}",),
                    ),
                ),
            )
        )
        resolution = self.knowledge.accept_proposal(
            receipt["proposals"][0]["proposal_id"], "desktop-user"
        )
        assert resolution.resource_id is not None
        return resolution.resource_id


if __name__ == "__main__":
    unittest.main()
