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
    DailyInsightService,
    InsightGenerationFailed,
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
    new_ulid,
)
from allday_asr.v3.ports.insight_generation import (
    DailyInsightModelResult,
    RelationshipInsightModelResult,
)


TEST_ROOT = Path(__file__).parent
NOW = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)


class _InsightGenerator:
    model_label = "fake-codex"
    producer_version = "test-1"
    prompt_version = "v36-test-prompt"
    extractor_version = "v36-test-extractor"

    def __init__(self, *, invent_evidence: bool = False) -> None:
        self.invent_evidence = invent_evidence
        self.daily_requests = []
        self.relationship_requests = []

    def generate_daily(self, request, effort):
        self.daily_requests.append(request)
        event_id = (
            "invented-event"
            if self.invent_evidence
            else request.source_events[0]["event_id"]
        )
        utterance_id = request.key_quotes[0]["utterance_id"]
        item = {
            "text": "今天确认了合同交付决定。",
            "evidence_event_ids": [event_id],
            "evidence_utterance_ids": [utterance_id],
        }
        narrative = {
            "what_happened": (item,),
            "decisions": (item,),
            "new_todos": (),
            "completed": (),
            "unresolved": (),
            "important_people_interactions": (item,),
            "memorable_quotes": (item,),
            "tomorrow_attention": (),
        }
        return DailyInsightModelResult(
            narrative=narrative,
            turn_id="turn-daily",
            reasoning_effort=effort,
            usage={"input_tokens": 10},
        )

    def generate_relationship(self, request, effort):
        self.relationship_requests.append(request)
        event_id = request.source_events[0]["event_id"]
        utterance_id = request.interactions[-1]["utterance_id"]
        return RelationshipInsightModelResult(
            observations=(
                {
                    "text": "近期围绕合同交付的互动增多。",
                    "confidence": 0.82,
                    "rationale": "当前窗口有合同事件和本人原话。",
                    "evidence_event_ids": [event_id],
                    "evidence_utterance_ids": [utterance_id],
                },
            ),
            turn_id="turn-relationship",
            reasoning_effort=effort,
        )

    def close(self):
        return None


class V36DailyInsightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TEST_ROOT / f"v36-insights-{uuid4().hex}"
        self.database = V3Database.open(self.root / "core.sqlite3")
        self.now = NOW

        def factory() -> SqliteUnitOfWork:
            return SqliteUnitOfWork(self.database, now=lambda: self.now.isoformat())

        self.factory = factory
        self.knowledge = KnowledgeArchitectureService(factory, now=lambda: self.now)
        self.corrections = CorrectionInvalidationService(factory, now=lambda: self.now)
        self.generator = _InsightGenerator()
        self.insights = DailyInsightService(
            factory, self.generator, now=lambda: self.now
        )
        self._seed_session(1, self.now - timedelta(days=8), "上周讨论合同范围")
        self._seed_session(2, self.now, "今天确认合同周五交付")
        with self.factory() as uow:
            uow.people.create_person(
                "person-a", "张同学", "known", self.now.isoformat()
            )
        self._link_person()
        self.knowledge.materialize_evidence(self.session_ids[1])
        self.knowledge.materialize_evidence(self.session_ids[2])
        self.old_event = self._event(
            1,
            "commitment",
            {
                "title": "张同学继续核对合同",
                "actor_person_id": "person-a",
                "related_person_ids": ["person-a"],
                "commitment_direction": "other_to_self",
                "topics": ["合同"],
            },
        )
        self.today_event = self._event(
            2,
            "decision",
            {
                "title": "合同周五交付",
                "actor_person_id": "person-a",
                "related_person_ids": ["person-a"],
                "topics": ["合同", "交付"],
            },
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_daily_summary_is_rebuilt_from_events_and_becomes_stale_after_correction(
        self,
    ) -> None:
        summary = self.insights.generate_daily(
            "2026-09-01", "Asia/Singapore", reasoning_effort="auto"
        )

        self.assertEqual(summary["revision"], 1)
        self.assertEqual(summary["objective"]["statistics"]["decision_count"], 1)
        self.assertEqual(summary["objective"]["statistics"]["unresolved_count"], 1)
        self.assertEqual(summary["provenance"]["reasoning_effort"], "low")
        request = self.generator.daily_requests[0]
        self.assertFalse(hasattr(request, "previous_summary"))
        self.assertEqual(request.source_events[0]["event_id"], self.today_event)

        self.corrections.correct_utterance(
            CorrectUtteranceCommand(
                utterance_id=self.utterance_ids[2],
                expected_revision=1,
                text="今天确认合同下周五交付",
                actor="desktop-user",
                speaker_track_id=self.track_ids[2],
                change_speaker=False,
                identity=SelfIdentity.UNKNOWN,
                change_identity=False,
            )
        )
        stale = self.insights.daily("2026-09-01", "Asia/Singapore")
        self.assertEqual(stale["derivation_status"], "stale")

        regenerated = self.insights.generate_daily("2026-09-01", "Asia/Singapore")
        self.assertEqual(regenerated["revision"], 2)
        self.assertEqual(regenerated["derivation_status"], "active")

    def test_invented_model_evidence_is_rejected_before_persistence(self) -> None:
        service = DailyInsightService(
            self.factory,
            _InsightGenerator(invent_evidence=True),
            now=lambda: self.now,
        )
        with self.assertRaisesRegex(InsightGenerationFailed, "invented"):
            service.generate_daily("2026-09-01", "Asia/Singapore")
        self.assertEqual(self.insights.list_daily(), ())

    def test_relationship_facts_and_observations_are_separate_and_reversible(
        self,
    ) -> None:
        report = self.insights.generate_relationship(
            "person-a", 30, "2026-09-01", "Asia/Singapore"
        )

        self.assertEqual(report["window_days"], 30)
        self.assertIn("interaction_frequency", report["verified_facts"])
        self.assertEqual(report["observations"][0]["confidence"], 0.82)
        self.assertEqual(report["provenance"]["reasoning_effort"], "low")
        self.assertNotIn("observations", report["verified_facts"])

        invented = dict(report["observations"][0])
        invented["evidence_event_ids"] = ["invented-event"]
        with self.assertRaisesRegex(ValueError, "invented"):
            self.insights.revise_relationship(report["report_id"], (invented,))

        corrected = dict(report["observations"][0])
        corrected["text"] = "近期互动主题集中在合同交付。"
        revised = self.insights.revise_relationship(report["report_id"], (corrected,))
        self.assertEqual(revised["revision"], 2)
        self.assertEqual(revised["created_by"], "desktop-user")
        self.assertEqual(
            self.insights.retract_relationship(report["report_id"])["status"],
            "retracted",
        )
        restored = self.insights.undo_relationship(report["report_id"])
        self.assertEqual(restored["status"], "active")
        self.assertEqual(restored["revision"], 4)

        with (
            self.database.transaction() as connection,
            self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"),
        ):
            connection.execute(
                "UPDATE relationship_observation_revisions "
                "SET status = 'retracted' WHERE report_id = ? AND revision = 1",
                (report["report_id"],),
            )

    def _seed_session(self, number: int, captured: datetime, text: str) -> None:
        if not hasattr(self, "session_ids"):
            self.session_ids = {}
            self.utterance_ids = {}
            self.track_ids = {}
        session_id = new_ulid()
        utterance_id = new_ulid()
        track_id = new_ulid()
        self.session_ids[number] = session_id
        self.utterance_ids[number] = utterance_id
        self.track_ids[number] = track_id
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
                    progress=1,
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
                    pipeline_version="v3.6-test",
                    input_revision=1,
                    status=ProcessingStatus.SUCCEEDED,
                    config_digest="config",
                    current_stage=None,
                    progress=1,
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
                    speaker_track_id=track_id,
                    session_id=session_id,
                    run_id=f"run-{number}",
                    label="SPEAKER_00",
                    source_artifact_id=f"artifact-{number}",
                    created_at=captured,
                )
            )
            uow.evidence.add_utterance(
                Utterance(
                    utterance_id=utterance_id,
                    session_id=session_id,
                    run_id=f"run-{number}",
                    source_artifact_id=f"artifact-{number}",
                    speaker_track_id=track_id,
                    original_speaker_track_id=track_id,
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

    def _link_person(self) -> None:
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
                        self.track_ids[number],
                        self.now.isoformat(),
                        self.now.isoformat(),
                    ),
                )
            connection.execute(
                """
                INSERT INTO person_cluster_links (
                  link_id, cluster_id, person_id, status, confidence, source,
                  revision, operation_id, created_at, updated_at
                ) VALUES ('link-1', 'cluster-1', 'person-a', 'active', 1,
                  'human', 1, 'seed-operation', ?, ?)
                """,
                (self.now.isoformat(), self.now.isoformat()),
            )

    def _event(self, number: int, kind: str, patch: dict[str, object]) -> str:
        receipt = self.knowledge.submit_generation(
            GenerationSubmission(
                layer=KnowledgeLayer.EVENT,
                producer="v36-fixture",
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
                        (self.utterance_ids[number],),
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
